# AWS staging root module

This credential-free OpenTofu/Terraform root module implements
[ADR 0002](../../docs/adr/0002-aws-staging-destination.md). It provisions the
AWS substrate only; it never deploys an application image, creates application
secret values, authenticates MEGAcmd, or enables GPU/publication effects.

Defaults are `eu-central-1`, one Amazon Linux 2023 x86_64 `t3a.medium`, a
40 GiB encrypted gp3 root disk, a separate 8 GiB encrypted integration-profile
disk, and private single-AZ RDS PostgreSQL 17.10 on `db.t4g.micro`. The private
disk keeps MEGAcmd state separate from the Patreon browser's signed-in Chromium
profile and idempotency SQLite state.

Set `github_actions_deploy_enabled = true` to add a keyless deployment identity
for `neuraln-cyber/gen-automation`. Its trust policy accepts GitHub OIDC tokens
only when the immutable owner ID is `310034173`, the immutable repository ID is
`1314605368`, the independent repository-name claim is exactly
`neuraln-cyber/gen-automation`, and the token comes from the
`Deploy staging control plane` workflow on `refs/heads/main`. Its permissions
can run `AWS-RunShellScript` only on this module's exact control-plane instance.
The customized GitHub subject must be exactly
`repo:neuraln-cyber@310034173/gen-automation@1314605368:ref:refs/heads/main`.
It cannot plan or apply the
infrastructure, read application secrets, access buckets, or assume the EC2
runtime role. The deployment role uses short-lived web-identity credentials;
never create AWS access keys for GitHub.

An AWS account can have only one IAM OIDC provider for GitHub. Leave
`github_actions_oidc_provider_arn = null` when this state should create it. If
the account already has one, set that variable to its exact ARN instead; the
module references it and verifies the GitHub issuer and `sts.amazonaws.com`
audience before creating the role.

## Files

- `backend.s3.tfbackend.example`: partial, non-secret S3 backend configuration.
- `terraform.tfvars.example`: reviewed non-secret inputs.
- `cloud-init.yaml.tftpl`: host preparation only; no application configuration
  or credentials.
- the `.tf` files: networking, storage, RDS, IAM, compute, DNS, monitoring,
  alarms, budget, and outputs.

For an AWS Organizations member account, set `budget_enabled = false`. Create
the monthly budget in the management account with a linked-account filter for
the staging account; AWS Budgets does not support cross-account SNS topics.
The budget sends actual-spend notifications at 50%, 80%, and 100%, plus a
forecast notification at 100%. The subscription must be confirmed before any
of those notifications can arrive.

The asset bucket automatically removes only abandoned upload attempts under
`staging/` after `abandoned_staging_retention_days` (seven days by default),
including their noncurrent versions. Lifecycle expiry never targets
`masters/`, `derivatives/`, `finished-set-archives/`, or
`publication-packages/`; those immutable, version-pinned objects remain
available to the application and operator.

Use the operational sequence and acceptance gates in
[the AWS staging runbook](../../docs/aws-staging-runbook.md).

## Adopting existing RDS log groups

Application/bootstrap logs and the RDS `postgresql` and `upgrade` export groups
use `log_retention_days` (30 days by default). New installations create the
export groups before RDS. Existing installations must adopt the exact groups
RDS already created before applying this change, or creation will conflict.

First verify the AWS account/Region, database identifier, current retention,
and stored bytes for each exact group with a read-only CloudWatch inventory.
Preserve a recoverable infrastructure-state snapshot and export any required
older diagnostics before shortening retention: raising retention afterward
does not recover expired events. RDS backups and database snapshots do not
preserve CloudWatch log events.

Import only groups that already exist and are not already in this state. For
the default `name_prefix`, the temporary configuration-driven import blocks
are below. Put only applicable blocks in a local `rds-log-imports.tf`; adjust
the exact IDs if the reviewed prefix differs. Do not import a missing group
or a group owned by another infrastructure state.

```hcl
import {
  to = aws_cloudwatch_log_group.postgresql["postgresql"]
  id = "/aws/rds/instance/gen-automation-staging-postgresql/postgresql"
}

import {
  to = aws_cloudwatch_log_group.postgresql["upgrade"]
  id = "/aws/rds/instance/gen-automation-staging-postgresql/upgrade"
}
```

Run `tofu plan -out=staging.tfplan` with the normal reviewed backend and inputs.
The retention rollout must show only applicable log-group imports, in-place
retention/tag updates, and creation of genuinely missing log groups, with
no database or EC2 replacement and no log-group deletion. Stop and investigate
any unrelated change. Apply only that reviewed plan after approval, verify
both groups' retention, and remove the temporary import file after successful
adoption. `skip_destroy = true` retains these diagnostic groups if they are
later removed from state; it does not disable event retention.

## Version policy

The root supports OpenTofu/Terraform `>= 1.10, < 2.0` and constrains the AWS
provider to the current reviewed `6.55.x` line. Validate with OpenTofu 1.12.x.
After the first networked `tofu init`, review and commit `.terraform.lock.hcl`
so every operator/CI platform selects the same checksummed provider build.

Official references:

- [OpenTofu S3 backend and native lock file](https://opentofu.org/docs/language/settings/backends/s3/)
- [OpenTofu provider requirements](https://opentofu.org/docs/language/providers/requirements/)
- [AWS provider registry](https://registry.terraform.io/providers/hashicorp/aws/latest)
- [Amazon Linux 2023 public AMI parameters](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/finding-an-ami-parameter-store.html)
- [RDS-managed master passwords](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-secrets-manager.html)

## Intentional deferrals

- The module does not apply itself.
- The credential-free bundle under `deploy/` later attaches Caddy, nginx, and
  the application containers to `gen-automation-deploy.target` using immutable
  image digests.
- IMDSv2 is enabled with response hop limit 1. The AWS-using control-plane
  container must use host networking and bind its application listener only to
  loopback; Caddy alone owns public ports 80/443 and proxies through the
  loopback nginx request guard. The two host-networked edge containers run as
  fixed non-root UIDs whose IPv4 IMDS access is blocked by the required host
  firewall unit. Patreon and any separate MEGA sidecars must stay on a private
  Docker bridge, without AWS credentials or the Docker socket, so they cannot
  obtain the EC2 role. MEGA work inside the control-plane image may use that
  host-networked process.
- Runtime and migration PostgreSQL roles are created in a one-off SSM session;
  the RDS master credential is never injected into the long-running app.
- The optional GitHub OIDC role is for reviewed main-branch application
  deployments only. Infrastructure plans and applies still require the
  separately authenticated infrastructure operator.
- Application-generated secret values and the optional X secret JSON are
  created outside IaC so plaintext cannot enter plan files or state.
- The one-time MEGAcmd and Patreon Chromium logins happen only after the
  encrypted profile volume and its isolated permission boundaries are verified.
- SaladCloud and semantic GPU infrastructure remain outside this AWS module.
