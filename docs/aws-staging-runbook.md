# AWS staging provisioning runbook

This runbook provisions the credential-free AWS substrate accepted in
[ADR 0002](adr/0002-aws-staging-destination.md). The module lives at
`infra/aws-staging`. No command in this document enables GPU allocation,
publication, Patreon automation, or MEGA delivery.

## 1. Operator prerequisites

Use OpenTofu 1.12.x and AWS provider 6.55.x. Authenticate with AWS IAM Identity
Center, `aws login`, or another short-lived ambient identity. Do not export or
write long-lived AWS access keys into this repository.

The operator identity needs permission to plan/apply the resources in this
module, plus `iam:PassRole` for the exact created EC2 role. AWS account payment,
Route53 domain ownership, and service quotas are owner actions.

For routine application deployments without repeated AWS sign-in, set
`github_actions_deploy_enabled = true`. Keep the reviewed default
`github_actions_repository = "neuraln-cyber/gen-automation"`, immutable owner ID
`310034173`, and immutable repository ID `1314605368`. The repository's GitHub
OIDC subject template must include those immutable IDs and emit exactly
`repo:neuraln-cyber@310034173/gen-automation@1314605368:ref:refs/heads/main`.
If this AWS account already has the GitHub IAM OIDC provider, set
`github_actions_oidc_provider_arn` to that exact ARN; otherwise leave it null so
this state manages the provider. This is a one-time infrastructure apply. It
does not create an IAM user or any long-lived AWS access key.

Create a separate private S3 state bucket before initializing this module:

- enable versioning and default server-side encryption;
- block all public access;
- restrict state and lock-object access to the infrastructure operator/CI role;
- enable CloudTrail data-event monitoring if available; and
- test recovery of an earlier state-object version.

The backend bucket is not either application bucket and is not managed by this
state. Copy `backend.s3.tfbackend.example` to `backend.s3.tfbackend` and
`terraform.tfvars.example` to `terraform.tfvars`. Both destination filenames
are gitignored.

## 2. Validate without changing AWS

From `infra/aws-staging`:

```shell
tofu version
tofu fmt -check -recursive
tofu init -backend-config=backend.s3.tfbackend
tofu validate
tofu plan -out=staging.tfplan
tofu show staging.tfplan
```

Confirm the plan contains exactly:

- one public x86_64 `t3a.medium` EC2 instance and one EIP;
- ingress rules for TCP 80/443 only, with no key pair and no TCP 22;
- IMDSv2 required with response hop limit 1;
- encrypted 40 GiB root and separate encrypted 8 GiB gp3 integration-profile
  EBS volume;
- two private RDS subnets, private single-AZ `db.t4g.micro`, PostgreSQL 17.10,
  RDS-managed master password, SSL enforcement, and seven-day backups;
- two distinct versioned, encrypted, public-blocked S3 buckets;
- exact-bucket IAM plus SSM, exact CloudWatch log group, RDS secret read, and
  optional exact X-secret rotation access;
- when enabled, one GitHub OIDC provider (or an explicit reference to the
  existing provider) and one main-branch deploy role whose `ssm:SendCommand`
  access is limited to `AWS-RunShellScript` on the exact control-plane instance;
- optional Route53 A record only when zone ID and hostname are both set;
- CloudWatch EC2/RDS alarms, SNS email, and monthly budget; and
- no secret value, private key, password, token, or application environment
  document in plan output.

When staging is an AWS Organizations member account, set
`budget_enabled = false` and create the budget in the management account with
a linked-account filter for staging. Budget SNS topics cannot be cross-account;
use a management-account topic or direct email notification.

Do not apply until this review and a cost estimate are approved. Plan files can
contain sensitive infrastructure data and are gitignored; delete them securely
after use.

## 3. Apply and infrastructure canary

When the owner explicitly authorizes the AWS change:

```shell
tofu apply staging.tfplan
```

Accept the SNS email subscription immediately. AWS Budgets is delayed and is a
notification, not a hard spend stop.

Use the `ssm_start_session_command` output. Do not create an SSH key or open port
22. In the SSM session, verify:

```shell
systemctl is-active amazon-ssm-agent docker amazon-cloudwatch-agent
systemctl status gen-automation-deploy.target
findmnt /var/lib/gen-automation/integration-profiles
stat -c '%a %u:%g' /var/lib/gen-automation/integration-profiles/mega
stat -c '%a %u:%g' /var/lib/gen-automation/integration-profiles/patreon-browser/profiles
stat -c '%a %u:%g' /var/lib/gen-automation/integration-profiles/patreon-browser/state
```

The expected MEGA and Patreon path permissions are all `700 10001:10001`,
matching the non-root service identity in their container images. Confirm
CloudWatch receives the cloud-init log and host metrics, then trigger/test one
alarm notification.

Verify both buckets have `Enabled` versioning, AES256 default encryption, all
four public-access blocks, and a TLS-only bucket policy. Run the repository's
opt-in S3 conformance canary against the asset bucket before any paid GPU job.

Verify RDS has no public address, is in the two-subnet DB subnet group, accepts
5432 only from the EC2 security group, and exposes an active
RDS-managed Secrets Manager secret. Perform a snapshot/restore drill before
calling staging recoverable.

### Keyless application deployment handoff

After the one-time apply, record the non-secret
`github_actions_deploy_role_arn` and `control_plane_instance_id` outputs as
GitHub Actions repository variables. A reviewed workflow on `main` can then
exchange its GitHub OIDC token for short-lived AWS credentials and deploy over
SSM without an AWS browser login or stored AWS keys.

The role trust requires `aud = sts.amazonaws.com`, the exact immutable subject
`repo:neuraln-cyber@310034173/gen-automation@1314605368:ref:refs/heads/main`,
the independent exact name claim `neuraln-cyber/gen-automation`, both immutable
numeric ID claims, the `Deploy staging control plane` workflow name, and
`refs/heads/main`. Its policy can create
commands only through the AWS-owned `AWS-RunShellScript` document on this one
instance. `GetCommandInvocation` uses `Resource = "*"` because that Systems
Manager API does not support resource-level IAM permissions; it does not grant
permission to create a command. The role cannot run OpenTofu, cancel unrelated
commands, read Secrets Manager, access S3, or assume the EC2 runtime role.

Keep command parameters free of passwords, tokens, and populated environment
files because SSM command text is audit-visible. The workflow should invoke
only the reviewed, root-owned host deployment helper with immutable image
digests and then wait for its health-checked result. Infrastructure plans and
applies remain a separate, explicitly authenticated operator action.

## 4. Database and application handoff

Use the RDS-managed bootstrap secret only from a bounded one-off SSM operation
to create distinct migration and runtime PostgreSQL roles. Require TLS with
certificate verification. Never inject the master credential into the
long-running container.

Create application-generated session, TOTP, and worker-signing secrets directly
in the chosen runtime secret store outside OpenTofu. Create the optional X OAuth
JSON outside OpenTofu, then set only its full ARN as `x_oauth_secret_arn` and
re-plan. Secret values must not enter tfvars, environment files, user-data,
plans, state, logs, issue trackers, or chat.

Deploy Caddy, nginx, and the pinned application images as systemd units wanted by
`gen-automation-deploy.target`. Run the AWS-using control-plane container with
host networking so IMDSv2 remains available with response hop limit 1, and bind
its application listener only to `127.0.0.1`. Caddy alone owns host ports
80/443 and proxies through an nginx request guard on `127.0.0.1:8080` to that
loopback listener. Run Caddy and nginx as fixed, distinct non-root host UIDs and
reject their IPv4 IMDS traffic with persistent OUTPUT owner rules. IPv6 IMDS is
disabled by the module.

Keep the Patreon browser and any separate MEGA uploader sidecars on a private
Docker bridge: never use host networking for them, never inject AWS
credentials, and never mount the Docker socket. The IMDS response hop limit
then prevents a bridged sidecar from obtaining the EC2 instance role. The
Patreon sidecar does not need AWS access. If MEGA delivery runs inside the
control-plane image instead of a sidecar, it may run in the same host-networked
controller process. Configure the app:

- use the private RDS TLS URL;
- use ambient EC2 identity for the asset bucket;
- leave explicit S3 access-key settings empty;
- use the exact asset bucket and `eu-central-1`;
- bind `/var/lib/gen-automation/integration-profiles/mega` into the uploader at
  `/run/gen-automation/mega-profile`;
- bind
  `/var/lib/gen-automation/integration-profiles/patreon-browser/profiles` to
  `/profiles` and
  `/var/lib/gen-automation/integration-profiles/patreon-browser/state` to
  `/state` in the UID/GID 10001 Patreon browser sidecar; and
- leave GPU allocation and external publication effects disabled.

The committed staging environment example uses the bounded single-owner
session profile: a 90-day absolute lifetime, a sliding 30-day idle lifetime,
and a 1-hour recent-authentication window for sensitive actions. Authentication
still uses password, TOTP, CSRF protection, opaque server-side sessions, and
login throttling. Apply this profile only to the owner's private device, log out
when deliberately ending a session, and revoke sessions if that device is lost.
Existing sessions retain the expiry established when they were created, so sign
in once after changing these values to receive the longer bounded session.

Before activation, point the final DNS name at the EIP. The committed nginx
guards are validated against the pulled image before the protected application
starts, so their two startup assertions remain true. After activation, verify
the Caddy certificate before enabling provider callbacks or any external
publication effect.

### Install the credential-free container bundle

The reviewed host bundle is in `infra/aws-staging/deploy`. Transfer that
directory to the instance through the approved SSM deployment path, verify it
matches the reviewed commit, and install it without starting containers:

```shell
cd /path/to/reviewed/infra/aws-staging/deploy
sudo ./install.sh
sudo cp /etc/gen-automation/examples/deploy.env.example /etc/gen-automation/deploy.env
sudo cp /etc/gen-automation/examples/control-plane.env.example /etc/gen-automation/control-plane.env
sudo cp /etc/gen-automation/examples/patreon-browser.env.example /etc/gen-automation/patreon-browser.env
sudo cp /etc/gen-automation/examples/caddy.env.example /etc/gen-automation/caddy.env
sudo chmod 0600 /etc/gen-automation/*.env
sudo chown root:root /etc/gen-automation/*.env
```

The installer pins Docker Compose v5.1.2 at
`/usr/local/lib/docker/cli-plugins/docker-compose` and verifies the committed
official-release SHA-256 before installing it. This removes any dependency on
whether the Amazon Linux 2023 `docker` package happens to bundle Compose.
Installation fails closed on a download, checksum, architecture, or resolved
version mismatch and does not start the application.

The committed files contain no secret values. Put only reviewed immutable
`repository@sha256:<64 lowercase hex>` image references in `deploy.env`.
Populate the host-only root-owned runtime files through the chosen secret
delivery procedure; never copy their populated form back into the repository,
SSM command text, user-data, Terraform, logs, or chat. Keep explicit AWS access
keys absent. The bundle's validated owner rules ensure only the
host-networked controller UID, not the Caddy/nginx edge UIDs, can reach IMDS.

Apply database migrations with the bounded migration role before activation.
Keep GPU allocation, Patreon publication, MEGA delivery, and X publication
disabled until their individual canaries pass. Then validate and start:

```shell
sudo /usr/local/libexec/gen-automation-validate-deployment
sudo docker compose \
  --env-file /etc/gen-automation/deploy.env \
  -f /opt/gen-automation/deploy/compose.yaml config --quiet
sudo systemctl enable --now gen-automation-staging.service
systemctl status gen-automation-staging.service
sudo docker compose \
  --env-file /etc/gen-automation/deploy.env \
  -f /opt/gen-automation/deploy/compose.yaml ps
curl --fail http://127.0.0.1:8000/api/v1/health/ready
curl --fail http://127.0.0.1:8090/health/live
```

The unit is wanted by `gen-automation-deploy.target`, pulls only the configured
digests, and lets Compose enforce health-gated startup: Patreon sidecar,
controller, nginx ingress guard, then Caddy. Confirm the controller, nginx, and
Caddy use host networking, Uvicorn listens only on `127.0.0.1:8000`, nginx only
on `127.0.0.1:8080`, Patreon publishes only `127.0.0.1:8090`, and Caddy alone
owns host ports 80/443. The bundle contains no privileged container or
Docker-socket mount. After pulling, the unit validates the Caddyfile and nginx
configuration inside the immutable images. Activation fails closed unless the
per-client request/connection limits, body/header bounds, timeouts,
forwarding-header replacement, and IPv4 IMDS owner blocks are active.

## 5. MEGA and destination canaries

Authenticate the official pinned MEGAcmd build once into the encrypted mounted
profile. Never place a MEGA email, password, session, folder key, or write key
in application configuration. Run the deterministic package upload/download
verification canary before enabling the MEGA destination.

Open the pinned Patreon browser sidecar only through an SSM-controlled
operator flow and sign in once using the persistent `/profiles` mount. Run
`sudo /usr/local/sbin/gen-automation-bootstrap-patreon-profile`, then follow the
loopback-only SSM port-forward sequence in
`docs/patreon-browser-publisher.md`. The sidecar's durable idempotency SQLite
database must use `/state`. Never copy the Chromium profile or cookies into an
image, user-data, tfvars, state, logs, or backups without an explicit
credential-handling decision.

Enable X only after the exact secret ARN policy, creator ID, sensitive-media
settings, human publication approval, and zero-effect canary have passed.

## State and teardown safety

State contains resource identifiers, the operator notification email, and the
RDS secret ARN, but not the RDS-managed password or application secret values.
Protect state as sensitive anyway. Never commit `.terraform/`, `*.tfstate`,
`*.tfplan`, copied backend config, or real tfvars.

Database deletion protection defaults on and final snapshots default on.
Disabling either requires an explicit reviewed plan. S3 `force_destroy` is
false, so buckets with retained masters/models cannot be silently destroyed.
The separate integration-profile volume is an IaC resource containing
credential-bearing MEGA and Patreon state: snapshot/encrypt/restrict it as
credential material before intentional replacement or teardown.
