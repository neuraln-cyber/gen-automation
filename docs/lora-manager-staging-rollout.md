# LoRA manager staging rollout

The staging LoRA manager is enabled only after its S3 browser-upload boundary,
runtime IAM grants, Civitai secret grant, and pinned worker manifest already
exist. The API key itself never enters Git, OpenTofu values or state, GitHub,
Systems Manager command text, host environment files, or logs. Only the full
non-secret Secrets Manager ARN is passed to the rollout.

## Required order

1. Create the Civitai API-key secret out of band. Retain only its exact full ARN
   for configuration; do not copy the API key into a shell variable or file.
2. In the ignored staging OpenTofu values, set `browser_upload_origin` to the
   exact HTTPS dashboard origin and set `civitai_api_secret_arn` to that exact
   ARN. Keep the six-character AWS-managed ARN suffix.
3. Plan and review the infrastructure before enabling the application. The plan
   must include all of these boundaries:

   - model-bucket CORS permits only `POST` from the exact dashboard origin and
     exposes `ETag` and `x-amz-version-id`;
   - the control-plane role can operate only on `onboarding/loras/*` and
     `worker/managed-loras/sha256/*` in the model bucket;
   - the worker artifact reader can read exact versions only under
     `worker/managed-loras/sha256/*`; and
   - the control-plane role can read only the configured Civitai secret ARN.

4. Apply that reviewed infrastructure plan and verify CORS and IAM before the
   application flag is enabled. Application deployment must not be used as a
   substitute for this infrastructure apply.
5. Populate the root-owned staging `control-plane.env` manifest inputs before
   rollout:

   - `GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_BUCKET`;
   - `GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_REGION=eu-central-1`;
   - `GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_JSON`; and
   - the independently reviewed 64-hex
     `GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_SHA256` trust anchor.

   The JSON's `manifest_sha256` must equal that independent anchor. The rollout
   fails without both values and does not derive or repair an absent anchor.
6. Set the non-secret GitHub repository variable
   `AWS_STAGING_CIVITAI_API_SECRET_ARN` to exactly the same ARN applied in step
   4. Only after verifying that apply, set the non-secret repository gate
   `AWS_STAGING_LORA_MANAGER_PREREQUISITES_APPLIED=true`. Do not create a GitHub
   secret containing the API key. An absent gate blocks the rollout before it
   obtains AWS credentials or changes the host.
7. Manually run the existing `Deploy staging control plane` workflow from the
   current tip of `main` with component `lora-manager` and operation `enable`.
   Keeping the operation inside this workflow preserves the exact workflow-name
   claim in the staging OIDC trust policy. Routine image deployments deliberately
   preserve the current toggle and never enable or disable it. The explicit job
   verifies the helper and deployment validator from its exact commit and refuses
   enablement unless the running control-plane image carries that same exact
   source-revision label. This prevents promotion from racing ahead of the normal
   schema and immutable-image deployment.
   Under the shared host update lock it stops the controller, preserves the full
   root-owned environment file, and atomically sets only:

   ```text
   GEN_AUTOMATION_LORA_MANAGER_ENABLED=true
   GEN_AUTOMATION_CIVITAI_API_SECRET_REFERENCE=aws-secrets-manager://<exact-configured-arn>
   ```

   The previous environment and validator are restored if configuration,
   deployment validation, restart, or readiness fails.

   The equivalent CLI dispatches are:

   ```shell
   gh workflow run deploy-staging.yml --ref main -f component=lora-manager -f operation=status
   gh workflow run deploy-staging.yml --ref main -f component=lora-manager -f operation=enable
   gh workflow run deploy-staging.yml --ref main -f component=lora-manager -f operation=disable
   ```

## Verification

After a successful rollout, verify the normal local readiness endpoint and use
the dashboard to create one bounded import job. Do not inspect or print the
secret value. Confirm that manual upload URLs target only the model bucket's
`onboarding/loras/<job-id>/source.safetensors` namespace and that completed
managed objects use
`worker/managed-loras/sha256/<full-digest>.safetensors` with an exact S3
VersionId.

If the infrastructure apply, exact ARN, private artifact bucket, manifest JSON,
or independent manifest trust anchor is missing, leave the manager disabled and
correct that prerequisite first. Never bypass the validator with an inline API
key.

For a safe read-only check, run the same workflow with component `lora-manager`
and operation `status`; it reports only whether the flag, secret reference, and
trust anchor are configured. For an emergency pause, use component `lora-manager`
and operation `disable`. Disablement needs neither the Civitai ARN nor the
prerequisite gate and does not inspect or contact Civitai. A later routine image
deployment preserves the disabled state.
