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
- optional Route53 A record only when zone ID and hostname are both set;
- CloudWatch EC2/RDS alarms, SNS email, and monthly budget; and
- no secret value, private key, password, token, or application environment
  document in plan output.

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
stat -c '%a %U:%G' /var/lib/gen-automation/integration-profiles/mega
stat -c '%a %u:%g' /var/lib/gen-automation/integration-profiles/patreon-browser/profiles
stat -c '%a %u:%g' /var/lib/gen-automation/integration-profiles/patreon-browser/state
```

The expected MEGA permissions are `700 root:root`; both Patreon paths are
`700 10001:10001`. Confirm CloudWatch receives the cloud-init log and host
metrics, then trigger/test one alarm notification.

Verify both buckets have `Enabled` versioning, AES256 default encryption, all
four public-access blocks, and a TLS-only bucket policy. Run the repository's
opt-in S3 conformance canary against the asset bucket before any paid GPU job.

Verify RDS has no public address, is in the two-subnet DB subnet group, accepts
5432 only from the EC2 security group, and exposes an active
RDS-managed Secrets Manager secret. Perform a snapshot/restore drill before
calling staging recoverable.

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

Deploy Caddy and the pinned application images as systemd units wanted by
`gen-automation-deploy.target`. Run the AWS-using control-plane container with
host networking so IMDSv2 remains available with response hop limit 1, and bind
its application listener only to `127.0.0.1`. Caddy alone owns host ports
80/443 and proxies to that loopback listener.

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

When DNS is enabled, verify the EIP A record and Caddy certificate before
asserting ingress rate limits/request guards or starting protected mode.

## 5. MEGA and destination canaries

Authenticate the official pinned MEGAcmd build once into the encrypted mounted
profile. Never place a MEGA email, password, session, folder key, or write key
in application configuration. Run the deterministic package upload/download
verification canary before enabling the MEGA destination.

Open the pinned Patreon browser sidecar only through an SSM-controlled
operator flow and sign in once using the persistent `/profiles` mount. The
sidecar's durable idempotency SQLite database must use `/state`. Never copy the
Chromium profile or cookies into an image, user-data, tfvars, state, logs, or
backups without an explicit credential-handling decision.

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
