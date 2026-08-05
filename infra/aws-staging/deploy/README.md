# AWS staging container bundle

This directory is a credential-free deployment bundle for the single staging
EC2 host. It does not build images, create secrets, authenticate MEGA or
Patreon, run database migrations, or enable external effects.

The five image references are supplied through `/etc/gen-automation/deploy.env`
and must be immutable `repository@sha256:<64 lowercase hex>` references:

- the control-plane image containing the pinned official MEGAcmd build;
- the Patreon browser sidecar image;
- the bounded semantic anatomy gateway image;
- a reviewed official nginx image; and
- a reviewed official Caddy image.

Service startup pulls those exact digests and validates the committed Caddy and
nginx configurations inside the same images before any container starts. Caddy
terminates TLS and replaces client-supplied forwarding headers. The loopback
nginx ingress guard enforces per-client global/authentication rate limits and
connection limits, an 8 MiB request-body ceiling (covering the supported 4 MiB
watermark upload plus multipart overhead), bounded header/body/proxy
timeouts, and a 32 KiB large-header ceiling.

The control plane, nginx, and Caddy use host networking. Uvicorn binds only
`127.0.0.1:8000`; nginx binds only `127.0.0.1:8080`; Caddy is the only process
binding public ports 80/443. Caddy and nginx run as dedicated host UIDs 10002
and 10003. A required persistent systemd oneshot installs and verifies OUTPUT
owner rules rejecting their traffic to IPv4 IMDS. The Patreon sidecar stays on
a Docker bridge and publishes port 8090 only on host loopback. The semantic
gateway uses a separate bridge and publishes port 8091 only on host loopback.
With EC2 IMDSv2 response hop limit 1, neither bridged sidecar can obtain the instance role,
while the host-networked control plane UID 10001 can use ambient AWS
credentials.

Caddy is pinned by digest, runs read-only as UID 10002, drops every capability
except `NET_BIND_SERVICE`, and is blocked from IMDS. It intentionally omits
`no-new-privileges` because that flag prevents the official Caddy binary's
reviewed bind-service file capability from becoming effective for its non-root
UID. The deployment preflight proves that this exact image can bind a low port
inside an isolated network namespace before public startup.

No service is privileged, no service mounts the Docker socket, and the Patreon
sidecar receives no AWS credentials. The persistent MEGA profile and the
Patreon Chromium/idempotency paths are the encrypted EBS mount prepared by the
AWS module.

Install from an SSM session:

```shell
sudo ./install.sh
sudo cp /etc/gen-automation/examples/deploy.env.example /etc/gen-automation/deploy.env
sudo cp /etc/gen-automation/examples/control-plane.env.example /etc/gen-automation/control-plane.env
sudo cp /etc/gen-automation/examples/migration.env.example /etc/gen-automation/migration.env
sudo cp /etc/gen-automation/examples/patreon-browser.env.example /etc/gen-automation/patreon-browser.env
sudo cp /etc/gen-automation/examples/semantic-gateway.env.example /etc/gen-automation/semantic-gateway.env
sudo cp /etc/gen-automation/examples/caddy.env.example /etc/gen-automation/caddy.env
sudo chmod 0600 /etc/gen-automation/*.env
```

`install.sh` also installs Docker Compose v5.1.2 for all users from Docker's
official release asset and verifies its committed SHA-256 before installation.
It downloads the official AWS RDS global CA bundle, verifies its reviewed
SHA-256, and mounts it read-only into the control plane. PostgreSQL URLs must
use `sslmode=verify-full` and
`sslrootcert=/run/gen-automation/rds-global-bundle.pem`.
It does not start the application. Re-running the installer re-verifies the
installed plugin and downloads it only when the reviewed binary is absent.
It also installs
`/usr/local/sbin/gen-automation-bootstrap-patreon-profile` and the atomic
`/usr/local/sbin/gen-automation-activate-semantic-gateway` command. The semantic
activator installs the private gateway with anatomy disabled, a zero assessment
cap, and the bounded staging cold-start policy of five total attempts with
30-to-120-second backoff. After configuring
the immutable images and environment files, use that one-time command with an
AWS SSM port-forward to `127.0.0.1:6080` to complete Patreon login in the
cloud-hosted browser. The executable sequence is in
`docs/patreon-browser-publisher.md`; no public VNC/noVNC ingress is required.

### Promote semantic anatomy beyond the canary

The gateway activator deliberately leaves anatomy disabled with a zero cap. After
the bounded canary has proved the pinned RunPod endpoint and gateway contract,
use the **Promote semantic anatomy coverage** GitHub Actions workflow. It uses the
existing short-lived GitHub OIDC deployment role and AWS Systems Manager; no
local AWS login or long-lived AWS key is needed.

Run `status` first, then `dry-run`, and finally `promote`. Promotion requires at
least one completed assessment for the current model/prompt profile, so a merely
healthy gateway cannot bypass the successful-canary gate. The default
per-scoring-run cap is 400 assessments, matching a typical master set, and the
promotion command accepts an explicit bound no higher than 1,000. Promotion always
keeps `shadow` mode, clears the canary UUID allowlist, and only permits the cap to
move upward. It does not enable enforcement or make a paid inference request as part
of the deployment command. The already-running background loop consumes the new
allowance, and the scale-to-zero provider may bill those assessments.

The equivalent command from a private root SSM session is:

```console
sudo /usr/local/sbin/gen-automation-promote-semantic-anatomy --status
sudo /usr/local/sbin/gen-automation-promote-semantic-anatomy \
  --dry-run --max-assessments 400 \
  --expected-control-plane-revision <40-hex-deployed-main-revision>
sudo /usr/local/sbin/gen-automation-promote-semantic-anatomy \
  --promote --max-assessments 400 \
  --expected-control-plane-revision <40-hex-deployed-main-revision>
sudo /usr/local/sbin/gen-automation-promote-semantic-anatomy --pause
```

The promotion is atomic and idempotent. It serializes against gateway activation
and control-plane updates, validates the existing gateway before mutation,
restarts the service, requires both gateway and controller readiness, and restores
the previous root-owned environment file if readiness fails. `status` reports only
non-secret mode/cap/allowlist-count and health values, current-profile assessment
counts by state, open-review totals, and the number of open-review images that do
not yet have a row for the current profile. It also projects the number of new
assessment rows under the promoted empty allowlist and the maximum provider-attempt
ceiling using the configured attempts per assessment, making spend exposure visible
before promotion. The projection applies the planned cap independently to each
scoring run, and promotion refuses an aggregate initial backlog above 1,000 new
assessment rows. The legacy environment variable retains `PER_PROFILE` in its name
for deployment compatibility. The GitHub workflow also binds
promotion to its exact deployed control-plane source revision, so dispatching while
the automatic rollout is still pending fails without changing configuration. Status
never prints asset IDs or a database credential.

Dispatch the workflow's `pause` operation at any time to stop new semantic
assessment work. It uses the same keyless GitHub OIDC/SSM path, requires no local
AWS login or RunPod key, does not wait for staging to catch up to the workflow
revision, and changes only `GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=false`.
Existing assessment and feedback rows, the configured cap, and the allowlist are
preserved. The command is atomic, restarts the controller, reports readiness,
and uses the same rollback path for configuration validation. After a disabled
configuration validates, pause never restores `enabled=true` merely because the
controller or gateway is already unhealthy; it reports both health states instead.
A later successful `promote` resumes processing.

After migrations complete, run the TTY-only initial owner enrollment from a
private interactive SSM session:

```console
sudo /usr/local/sbin/gen-automation-bootstrap-owner
```

The installer creates this wrapper alongside the Patreon bootstrap command.
The password, one-time TOTP provisioning data, and confirmation code remain
inside that terminal and are never accepted as command arguments.

Populate the five files out-of-band. Keep external effects disabled through the
first health, storage, MEGA, Patreon, and X canaries. The validator reads only
the minimum non-secret deployment invariants and never sources an environment
file. The systemd unit validates inputs, renders Compose, pulls the four exact
digests, validates both proxy configurations offline, then starts the
health-gated service chain: Patreon, controller, nginx, and Caddy.

```shell
sudo /usr/local/libexec/gen-automation-validate-deployment
sudo docker compose \
  --env-file /etc/gen-automation/deploy.env \
  -f /opt/gen-automation/deploy/compose.yaml config --quiet
sudo systemctl enable --now gen-automation-staging.service
```

Inspect health and ordering with:

```shell
systemctl status gen-automation-staging.service
systemctl status gen-automation-imds-egress.service
docker compose \
  --env-file /etc/gen-automation/deploy.env \
  -f /opt/gen-automation/deploy/compose.yaml ps
curl --fail http://127.0.0.1:8000/api/v1/health/ready
curl --fail http://127.0.0.1:8080/api/v1/health/ready
curl --fail http://127.0.0.1:8090/health/live
curl --fail http://127.0.0.1:8091/health/ready
```

To deploy a reviewed digest update, replace only the affected immutable image
reference and restart the unit. Roll back by restoring the previous digest and
restarting again.

Routine control-plane updates do not require an operator AWS login. After the
one-time GitHub OIDC role and repository variables are configured, a successful
`Publish immutable images` run for the current tip of `main` triggers
`.github/workflows/deploy-staging.yml`. That workflow resolves the published
`control-plane-mega` digest, verifies its exact source-revision label, exchanges
GitHub's short-lived OIDC identity for the staging role, and runs a bounded,
one-off Alembic migration container with the host-only `migration.env`. The
migration container is read-only, non-root, capability-free, time-limited,
isolated from the EC2 instance role by bridge networking, and receives no
secret value in the SSM command. A root-owned preflight requires
`migration.env` to contain only one TLS-verified database URL and proves its
username differs from the continuously running application role. Only after the
migration succeeds does the workflow invoke this root-owned host command
through SSM:

```shell
sudo /usr/local/sbin/gen-automation-update-control-plane \
  --image ghcr.io/neuraln-cyber/gen-automation/control-plane-mega@sha256:<64-hex> \
  --revision <40-hex-main-revision>
```

The update command accepts no credentials. It serializes updates with `flock`,
pulls and verifies the immutable linux/amd64 image and revision label,
atomically changes only `GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE`, validates
Compose, restarts the staging unit, and waits for local readiness. Any failed
validation or readiness check restores the previous environment atomically and
restarts the previous image. The update command itself never runs database
migrations or changes external-effect settings. Schema changes must remain
backward-compatible with the previous application image for the rollback
window; destructive changes require an expand-and-contract release sequence.

Set these non-secret GitHub repository variables once:

- `AWS_STAGING_DEPLOY_ROLE_ARN`: the OIDC role trusted only for this repository's
  immutable owner ID `310034173`, immutable repository ID `1314605368`, exact
  `neuraln-cyber/gen-automation` name claim, deployment workflow, and `main`
  branch;
- `AWS_STAGING_INSTANCE_ID`: the staging EC2 instance ID.

The role needs only `ssm:SendCommand` for that instance and the
`AWS-RunShellScript` document, plus the read APIs required to poll its own
command. Do not configure IAM-user access keys as repository secrets. The
instance continues to pull with its existing host-side registry access; no
registry token, application secret, or integration credential is placed in SSM
command text.

Before enabling the repository variables, verify the host can pull the current
immutable `control-plane-mega` digest as root. The package may be public, or the
host may retain a dedicated read-only GHCR credential; do not rely on a
developer's short-lived interactive login. The updater performs the same pull
as a bounded preflight and makes no deployment change if registry access fails.
