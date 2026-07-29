# Patreon browser publisher

The optional Patreon publisher drives Patreon's official creator UI from a
separate Playwright/Chromium sidecar. It consumes the exact deterministic
Patreon ZIP already registered by the publication runtime. The control-plane
image contains no browser and no Patreon login material.

When the sidecar is disabled, the existing package download and manual publish
flow is unchanged.

## Runtime contract

1. The controller re-reads the immutable package version, size, and SHA-256.
2. Immediately before the sidecar call it rechecks the durable publication
   approval/guard and records a provider-request start event.
3. The sidecar authenticates the controller with an HMAC-bound request identity,
   verifies the ZIP digest, canonical manifest, decoded image metadata, complete
   public-preview attestation, tier, tags, and schedule before opening Patreon.
   Scheduled intents are sent immediately after approval while that timestamp
   is still in the future; Patreon, rather than the controller queue, owns the
   later publication time.
4. Chromium uses one named persistent profile and uploads only `content/` to the
   paid, audience-locked image gallery. Patreon's current image-post editor has
   optional Preview Text, not a separate preview-image upload control, so the
   automatic path never exposes the designated manual-fallback preview as
   public media.
5. A confirmed `/posts/<slug>-<id>` URL becomes `PUBLISHED`. Login, CAPTCHA/2FA,
   a missing tier, or a changed UI becomes `NEEDS_OPERATOR` and preserves the
   manual ZIP. A lost confirmation becomes `UNKNOWN`. A definite pre-submit
   failure goes directly to `AWAITING_HUMAN` with the manual ZIP available.

There is no automatic retry after the browser request begins. A crash, timeout,
connection loss, malformed success response, or expired lease is `UNKNOWN`
because the publish click may have succeeded. Reconcile that intent in the
dashboard before taking any further action.

For an `UNKNOWN` Patreon intent, check the creator page before choosing either
dashboard action:

- **Confirm post exists** requires the verified Patreon post ID, canonical post
  URL, and a short record of the evidence checked. It marks the frozen intent
  published without invoking the sidecar.
- **Confirm absent and use manual package** requires evidence that no post was
  created. It moves the existing attempt to the manual handoff and makes the
  immutable ZIP downloadable. It does not create another attempt, reuse the
  sidecar request identity, click publish, or perform any provider effect.

Both actions are publisher-authorized, exact-digest and lock-version checked,
idempotent, and append a durable reconciliation plus audit event. The REST API
exposes the same operations at
`POST /publication-intents/{intent_id}:confirm-present` and
`POST /publication-intents/{intent_id}:confirm-absent`.

## Deployment

Build `Dockerfile.patreon-browser` and deploy the resulting image beside the
controller on a private service network:

- expose port `8090` only to the controller;
- run exactly one replica with an init process and no public ingress;
- mount encrypted persistent volumes at `/profiles` and `/state`,
  readable/writable only by UID/GID `10001`;
- keep `/var/lib/patreon-browser` ephemeral. `/state/idempotency.sqlite3` must
  survive sidecar restarts so an unresolved publish is never executed again;
- allow outbound HTTPS only to Patreon and required static/CDN hosts;
- never place a Patreon username, password, cookie, token, or profile archive in
  environment variables, logs, CI artifacts, or this repository.

Sidecar environment:

```text
GEN_AUTOMATION_PATREON_BROWSER_PROFILE_ROOT=/profiles
GEN_AUTOMATION_PATREON_BROWSER_SPOOL_ROOT=/var/lib/patreon-browser
GEN_AUTOMATION_PATREON_BROWSER_STATE_PATH=/state/idempotency.sqlite3
GEN_AUTOMATION_PATREON_BROWSER_PROFILE_REFERENCE=creator-main
GEN_AUTOMATION_PATREON_BROWSER_SHARED_SECRET=<deployment-injected-random-secret>
GEN_AUTOMATION_PATREON_BROWSER_EDITOR_URL=https://www.patreon.com/posts/new
GEN_AUTOMATION_PATREON_BROWSER_HEADLESS=true
GEN_AUTOMATION_PATREON_BROWSER_ACTION_TIMEOUT_SECONDS=180
GEN_AUTOMATION_PATREON_BROWSER_MAX_PACKAGE_BYTES=167772160
```

Controller environment:

```text
GEN_AUTOMATION_PATREON_BROWSER_PUBLISHING_ENABLED=true
GEN_AUTOMATION_PATREON_BROWSER_SIDECAR_URL=http://patreon-browser:8090/v1/publish
GEN_AUTOMATION_PATREON_BROWSER_PROFILE_REFERENCE=creator-main
GEN_AUTOMATION_PATREON_BROWSER_SHARED_SECRET=<same-deployment-injected-random-secret>
GEN_AUTOMATION_PATREON_BROWSER_TIMEOUT_SECONDS=240
```

Generate one random secret containing at least 32 bytes and inject the same value
into both services from deployment secret storage. It authenticates only this
internal channel; it is not a Patreon credential and must not be committed,
logged, or placed in an image.

The controller publication timeout must exceed the sidecar timeout, and its
lease must exceed the publication timeout.

## One-time account bootstrap

After the creator page is Adult/18+ classified and required identity/age
verification is complete:

1. From one SSM shell on the staging instance, run:

   ```bash
   sudo /usr/local/sbin/gen-automation-bootstrap-patreon-profile
   ```

   The command stops the normal stack to prevent concurrent profile use and
   starts the same pinned browser image with its encrypted `/profiles` mount.
2. From a second terminal, create an SSM-only port forward (replace the instance
   ID):

   ```bash
   aws ssm start-session \
     --target i-0123456789abcdef0 \
     --document-name AWS-StartPortForwardingSession \
     --parameters '{"portNumber":["6080"],"localPortNumber":["6080"]}'
   ```

3. Open
   `http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=remote`. The Chromium
   process and persistent user-data directory `/profiles/creator-main` are on
   the cloud instance; the local browser only displays the private session.
4. The account owner signs in and personally completes password, 2FA, CAPTCHA,
   and Patreon consent or verification prompts. Do not paste credentials into
   the SSM shell.
5. Press Ctrl+C in the first SSM shell. The command closes Chromium cleanly,
   removes the temporary noVNC container/network, and restarts the normal
   headless stack if it was previously running. Port 6080 is bound only to the
   instance loopback interface and is never opened by the security group.
6. Run one low-risk staging/canary intent. Confirm the exact tier, locked content
   images, tags, Set publish date behavior, post ID, and URL before enabling
   routine publication. Also exercise the manual package's separately attested
   preview workflow once.

Patreon UI selectors can change without notice. `editor_contract_changed` and
the other `NEEDS_OPERATOR` codes are fail-closed signals to recalibrate the
small Patreon-specific adapter against the signed-in account; they never trigger
a blind retry.

No Patreon API key is required for browser publication. A separate read-only API
client is optional later for post reconciliation.
