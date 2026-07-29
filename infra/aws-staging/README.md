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

## Files

- `backend.s3.tfbackend.example`: partial, non-secret S3 backend configuration.
- `terraform.tfvars.example`: reviewed non-secret inputs.
- `cloud-init.yaml.tftpl`: host preparation only; no application configuration
  or credentials.
- the `.tf` files: networking, storage, RDS, IAM, compute, DNS, monitoring,
  alarms, budget, and outputs.

Use the operational sequence and acceptance gates in
[the AWS staging runbook](../../docs/aws-staging-runbook.md).

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
- Application-generated secret values and the optional X secret JSON are
  created outside IaC so plaintext cannot enter plan files or state.
- The one-time MEGAcmd and Patreon Chromium logins happen only after the
  encrypted profile volume and its isolated permission boundaries are verified.
- SaladCloud and semantic GPU infrastructure remain outside this AWS module.
