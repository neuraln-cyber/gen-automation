# A14B private registry authorization

Use this one-use handoff only after the A14B cutover reports that the exact
private VIDEO deployment is awaiting registry authorization. It does not
submit an image-to-video job.

Prerequisites:

- `gh` is authenticated as `neuraln-cyber` with `read:packages`;
- the active AWS identity is the staging deployer in account `861912887470`;
- that local SSO role can call `sts:GetCallerIdentity`, `ssm:PutParameter` and
  `ssm:DeleteParameter` on
  `/gen-automation-staging/a14b/ghcr-pull-once`, plus `ssm:SendCommand` on the
  exact staging instance and `ssm:GetCommandInvocation` for its submitted
  command; and
- the repository virtual environment contains the reviewed current code.

Run the helper with only the immutable image and deployment identifiers:

```powershell
.\scripts\provision-a14b-private-staging.ps1 `
  -Image 'ghcr.io/neuraln-cyber/gen-automation/video-worker-a14b-private@sha256:<64-hex>' `
  -DeploymentId '<cutover deployment UUID>'
```

The helper does not accept a caller-supplied instance ID. It resolves
`control_plane_instance_id` from the reviewed `infra/aws-staging` OpenTofu state
under the fixed `gen-automation-staging` profile and `eu-central-1` region,
strictly validates the resulting `i-...` value, and passes it to Python for a
second validation before `SendCommand`.

The helper captures the GitHub credential through a fixed, non-shell child
process and never places it in an argument, environment variable, file, or
log. Before writing AWS state it verifies the GitHub login, `read:packages`
scope, and exact authenticated GHCR manifest bytes. It creates the fixed SSM
parameter as a non-overwritable `SecureString` with a maximum ten-minute UTC
expiry, sends only its name/version and public deployment identifiers to the
host, and deletes the parameter only after observing `Success` bound to the
exact command and instance with the expected response code, output, and empty
stderr. `Failed`, `Cancelled`, `TimedOut`, or malformed `Success` results remain
ambiguous because the host-side `docker exec` or provider POST may have outlived
the SSM client.

If command submission returns no valid command ID, status polling fails or
times out, or cleanup itself cannot be verified, the helper retains—or may
retain—the fixed non-overwritable parameter and prints only generic manual
inspection guidance. Inspect the command and private VIDEO group before any
manual cleanup or retry. Do not delete the parameter while an unobserved command
could still read it, and never issue a blind second provider POST.

If the fixed parameter already exists, the helper fails without reading,
overwriting, or deleting it. Treat that as a stale-or-active handoff: inspect
the submitted SSM command and private VIDEO group, wait until no command can
still consume the value, confirm its embedded expiry has passed through an
approved break-glass process, and only then delete the exact parameter. Never
blindly retry an ambiguous provider POST. The host accepts only the fixed name,
exact returned version, matching image digest and username, exact JSON keys,
fresh parameter metadata, and an unexpired payload.
