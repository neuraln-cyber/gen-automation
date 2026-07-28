# ADR 0002: AWS staging destination

- Status: accepted
- Date: 2026-07-28

## Context

The MVP needs a small always-on control plane, durable PostgreSQL state,
private versioned asset and model storage, workload-identity access to AWS
Secrets Manager, and a persistent private volume for the authenticated
MEGAcmd profile. GPU generation remains ephemeral on SaladCloud.

The staging destination should minimize operator work and idle cost without
putting the authoritative workflow database on the application host.

## Decision

Deploy staging in AWS `eu-central-1` (Frankfurt):

- one x86-64 EC2 `t3a.medium` control-plane host;
- an encrypted 40 GiB gp3 root volume;
- a separate encrypted 8 GiB gp3 volume for the MEGAcmd profile;
- single-AZ RDS for PostgreSQL on `db.t4g.micro`, with 20 GiB storage and
  seven-day automated backup retention;
- separate private, versioned S3 buckets for generated assets and model
  artifacts;
- AWS Secrets Manager for the X OAuth credential document;
- an EC2 instance profile with narrowly scoped S3, Secrets Manager, logging,
  and deployment permissions instead of static AWS credentials;
- Systems Manager Session Manager for administration, with no public SSH port;
- Caddy for HTTPS ingress and Route 53 for DNS;
- CloudWatch logs, service alarms, and AWS budget notifications.

The RDS instance and its security group remain private. The EC2 security group
accepts only HTTP/HTTPS ingress; administrative access uses Systems Manager.
SaladCloud remains the GPU execution destination and scales to zero outside
generation work.

## Consequences

- The fixed staging cost is expected to be approximately USD 60-80 per month
  before GPU use, storage growth, data transfer, tax, and existing third-party
  subscriptions.
- The application can use temporary EC2 role credentials for S3 and Secrets
  Manager without long-lived AWS access keys.
- The MEGAcmd session cache survives container replacement on its dedicated
  encrypted volume.
- PostgreSQL backups and point-in-time recovery are managed independently of
  the application host.
- Staging has a single control-plane host and a single-AZ database. Multi-AZ,
  multiple control-plane replicas, and cross-region recovery remain production
  hardening steps.
- Infrastructure must be provisioned reproducibly and canaried before
  production resources are created.

## Alternatives considered

- PostgreSQL on the EC2 host: lower fixed cost, but couples database recovery
  and application-host failure for a relatively small saving.
- ECS/Fargate plus EFS: stronger service abstraction, but materially more
  moving parts and baseline cost for a one-operator staging deployment.
- Lightsail: simple pricing, but less natural workload-identity integration
  with the existing S3 and Secrets Manager contracts.
- A GPU-provider host for the control plane: does not provide the same managed
  database, secret, identity, and durable-volume integration; GPU execution is
  already isolated behind the provider-neutral worker contract.
