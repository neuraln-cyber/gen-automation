# Staging X OAuth 1.0a runtime operations

This procedure attaches an existing staging OAuth 1.0a secret to the running
controller. It does not collect or transfer any X credential value. The only
command-line inputs are the complete Secrets Manager ARN and the expected
numeric X user ID; both are non-secret identifiers.

Use this procedure only for staging account `861912887470` in
`eu-central-1`. The helper rejects every other account, region, secret name,
AWS identity, instance target, and authentication mode.

## Prerequisites

Before running the helper, confirm all of the following:

- The reviewed application revision and OAuth 1.0a runtime check are deployed,
  and the staging controller is healthy. The local checkout must be that same
  reviewed revision; the helper carries and verifies its strengthened
  deployment validator during configuration.
- The X app has **Read and write** permission. Its owner OAuth 1.0a Access
  Token and Secret were regenerated after that permission was selected.
- `scripts/bootstrap-x-oauth.ps1 -OAuth1` has already created the exact
  `gen-automation-staging/x/oauth1` secret. Retain only its two non-secret
  outputs: the complete secret ARN and numeric creator user ID. See
  [Access and secrets](access-and-secrets.md#x-destination) for the bootstrap
  and secret schema.
- OpenTofu has been applied with that exact ARN as `x_oauth_secret_arn` and
  `x_oauth_auth_mode = "oauth1"`. This gives the controller instance role only
  `DescribeSecret` and `GetSecretValue` on the exact secret.
- The local reviewed OpenTofu state is initialized and contains the
  `control_plane_instance_id` output.
- AWS CLI, OpenTofu, and Windows PowerShell are installed. The fixed
  `%TEMP%\gen-automation-staging-aws-config` file defines profile
  `gen-automation-staging`, and that profile is signed in as the approved
  `GenAutomationStagingDeployer` IAM Identity Center role.
- The durable publication guard is stopped, with no publication attempt or
  step currently processing. The remote helper verifies this condition before
  it changes configuration or makes the account-binding request.

The Windows operator needs `sts:GetCallerIdentity`, `ssm:SendCommand` for the
AWS-owned `AWS-RunShellScript` document and exact staging instance, and
`ssm:GetCommandInvocation`. The operator needs read access to the reviewed
OpenTofu state but does not need permission to read the X secret. The EC2
controller role, not the operator, performs the exact secret read.

## 1. Authenticate and dry-run

Run these commands from a Windows PowerShell at the repository root. Substitute
only the two non-secret values printed by the OAuth 1.0a bootstrap:

```powershell
Set-Location D:\Code\gen-automation
$env:AWS_CONFIG_FILE = "$env:TEMP\gen-automation-staging-aws-config"
aws sso login --profile gen-automation-staging

$secretArn = "arn:aws:secretsmanager:eu-central-1:861912887470:secret:gen-automation-staging/x/oauth1-AbCd12"
$creatorUserId = "1234567890123456789"

.\scripts\configure-x-oauth1-staging.ps1 `
  -DryRun `
  -SecretArn $secretArn `
  -CreatorUserId $creatorUserId
```

The example suffix and user ID above are placeholders. Do not substitute any
of the four private OAuth 1.0a credential values or the secret JSON. Never put
those values in a command, environment variable, tfvars, log, screenshot,
issue, chat, or SSM command.

The dry-run verifies the fixed AWS identity, resolves the exact instance from
reviewed OpenTofu state, validates the helper and deployment validator, and
prints both SHA-256 digests. It does
not submit an SSM command, contact X, restart the controller, or modify AWS or
the instance.

## 2. Configure and verify the binding

After the dry-run succeeds, use the same two non-secret variables:

```powershell
.\scripts\configure-x-oauth1-staging.ps1 `
  -Configure `
  -SecretArn $secretArn `
  -CreatorUserId $creatorUserId
```

The bounded SSM operation:

1. verifies the reviewed helper digest, deployment files, root-only
   configuration permissions, controller health, and stopped publication
   guard;
2. acquires the deployment locks and creates a root-only `0600` rollback copy
   under `/etc/gen-automation`;
3. atomically changes only `GEN_AUTOMATION_X_AUTH_MODE`,
   `GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE`, and
   `GEN_AUTOMATION_X_CREATOR_USER_ID`;
4. validates the deployment, restarts the controller, waits for readiness, and
   confirms the running process has the exact requested settings; and
5. reads `AWSCURRENT` through the controller role and performs signed
   `GET /2/users/me`, succeeding only when X returns the expected numeric user
   ID; then
6. atomically installs and executes the exact strengthened deployment validator
   carried from the reviewed checkout.

Step 5 is a zero-post account-binding check. It does not upload media, create a
post, schedule a post, or enable the publication guard. If the exact settings
are already present, the operation avoids rewriting the environment and avoids
an unnecessary restart, but it still validates health, safety, settings, and
the account binding before reporting success.

If any post-edit validation, restart, readiness, binding, or validator check
fails, the helper restores the prior validator and environment atomically,
validates the restored deployment, restarts the controller, and waits for
health. It removes the backups after either a healthy success or healthy
rollback. A root-only backup is retained only when automatic rollback itself
cannot be made healthy; stop and repair the host in that case.

## 3. Recheck without reconfiguring

The separate canary revalidates the currently configured account at any later
time:

```powershell
.\scripts\configure-x-oauth1-staging.ps1 -Canary
```

This operation accepts no ARN or user-ID argument. It does not rewrite the
environment or restart the controller. It verifies the deployment, controller
health, stopped publication guard, exact OAuth 1.0a reference and creator ID,
then performs only the signed `GET /2/users/me` account-binding request. Its
successful result is:

```text
X OAuth 1.0a account binding passed. No media was uploaded and no post was created.
```

## What the zero-post checks do not prove

The account-binding GET proves that the stored credential is readable, its
OAuth 1.0a signature is accepted, and it belongs to the configured creator ID.
It deliberately does not prove media-upload or post-creation permission,
billing availability, posting limits, or X policy acceptance.

Test those external effects only in a later, separately reviewed canary with
fresh owner approval. Use one clearly safe-for-work image and caption, keep the
destination and publication guard bounded to that approved effect, and verify
the resulting account, media label, and post before enabling normal X
delivery. A successful zero-post canary is not authorization to run that write
canary.
