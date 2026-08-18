# Artifact and workflow onboarding

`gen-automation-artifacts` is the one-off operator path for checkpoints,
diffusion models, LoRAs, model-family support files, the optional face
detector, and the bundled ComfyUI workflows. Adding an artifact is an
edit-and-rerun operation; no database row or worker manifest is written by
hand.

The command:

1. resolves the one active owner, or the `owner_username` named in the plan;
2. hashes and format-checks each mounted `.safetensors` or detector `.pt` file
   with the same validators used during GPU-worker bootstrap;
3. when `base_manifest_path` is set, validates the self-verifying currently
   deployed catalog and rechecks every retained artifact at its exact object
   key and immutable VersionId;
4. verifies that each new worker-artifact object key already has the same byte
   size and `sha256` object metadata, then pins its non-null object VersionId in
   the catalog;
5. unions retained and new entries deterministically, rejecting changed or
   colliding identities, logical names, object keys, and runtime targets;
6. parses each workflow with duplicate-key, depth, item-count, and size bounds,
   then rejects every node class outside the worker allowlist;
7. requires the workflow SHA-256 in its content-addressed object key, uploads
   it with a create-only write, or verifies the existing object byte-for-byte;
8. creates or reaffirms the current primary-model, LoRA, and workflow
   compliance approvals through the existing audited registry service while
   retaining the explicit `model_family`; and
9. writes canonical `ArtifactManifest` catalog JSON plus its separate SHA-256
   trust anchor.

It never proxies a multi-gigabyte model through FastAPI. Primary models,
LoRAs, text encoders, VAEs, and detector files cross the large-file boundary
directly into the private worker artifact bucket. This keeps memory use and
cloud egress predictable.

## Prerequisites

- Run all database migrations and create an active owner account.
- Enable object versioning on private workflow storage. Runtime workflow reads
  require an exact version and ETag.
- Give the one-off job operator-scoped temporary read/write access only to the
  exact workflow and worker-artifact keys in the plan. The runtime
  control-plane role deliberately has no broad model-object read and must not be
  reused for onboarding.
- Put credentials only in the existing environment/secret bindings. The CLI
  accepts paths, never credentials.

Required environment:

```text
GEN_AUTOMATION_DATABASE_URL
GEN_AUTOMATION_STORAGE_BUCKET
GEN_AUTOMATION_STORAGE_REGION
GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_BUCKET
GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_REGION
```

The existing optional endpoint and credential bindings are also supported:

```text
GEN_AUTOMATION_STORAGE_ENDPOINT_URL
GEN_AUTOMATION_STORAGE_ACCESS_KEY_ID
GEN_AUTOMATION_STORAGE_SECRET_ACCESS_KEY
GEN_AUTOMATION_STORAGE_SESSION_TOKEN
GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_ENDPOINT_URL
GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_ACCESS_KEY_ID
GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_SECRET_ACCESS_KEY
GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_SESSION_TOKEN
```

When explicit keys are omitted, the normal cloud role/SDK credential chain is
used. Do not place secrets in the plan, command line, generated manifest, or
terminal transcript.

After onboarding, copy every exact worker-artifact key and returned non-null S3
VersionId into the OpenTofu
`salad_worker_artifact_object_versions` map. Applying that map creates the
disabled-by-default Salad reader role. OpenTofu sorts the map and renders one
compact inline policy with no statement Sids: one statement per exact object
key and matching `s3:VersionId`, followed by the existing content-addressed
managed-LoRA prefix. Every statement permits only `s3:GetObjectVersion`. A
resource precondition measures the exact minified document and blocks the plan
if it exceeds IAM's 10,240-character aggregate inline-policy quota for the
dedicated reader role. Apply every artifact-map change from a reviewed saved
plan only with no active image jobs, warm lease, queued provider work, or GPU
replica. It never grants an unversioned read. Copy the reader role's nonsecret
output ARN to
`GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_ROLE_ARN`.

### One-time state reconciliation after the interrupted shard rollout

The superseded customer-managed-policy rollout removed
`aws_iam_role_policy.salad_worker_artifact_reader` from OpenTofu state before it
failed. The exact inline policy was restored directly with `PutRolePolicy`, so
live IAM has the policy while the state does not. Do not run a general apply in
that condition, and never recreate or retry saved plan
`63e269a364bcfb95274e278a6e961cd37faf8e027719989af09a61de79bb630a`.

After this inline-policy configuration is merged, reconcile it in a dedicated
maintenance window:

1. Re-run the normal zero-work gate and take a recoverable version of the
   remote state.
2. Read state and IAM without mutation. Confirm that the inline policy named
   `gen-automation-staging-pinned-artifacts` exists on role
   `gen-automation-staging-salad-artifact-reader`, that the inline resource
   address is absent from state, and that no shard or managed-LoRA policies are
   attached to the role. Stop on any different preimage.
3. From the initialized `infra/aws-staging` root, make the only state mutation:

   ```powershell
   tofu import 'aws_iam_role_policy.salad_worker_artifact_reader[0]' 'gen-automation-staging-salad-artifact-reader:gen-automation-staging-pinned-artifacts'
   ```

   This import adopts the existing policy at the AWS provider's documented
   `role_name:role_policy_name` ID; it must not issue an IAM policy write.
4. Generate a new narrowly reviewed saved plan. With an unchanged artifact
   map, it must show no IAM content change and no managed-policy or attachment
   creation. Keep unrelated infrastructure drift out of the plan.

## Upload the large files

Upload each large file directly and attach its lowercase hexadecimal SHA-256
as S3 user metadata. Prefer a new, versioned object key when bytes change.
For example, from a restricted cloud operator job:

```bash
artifact_sha256="$(sha256sum /mnt/models/illustrious-v1.safetensors | cut -d' ' -f1)"
aws s3 cp /mnt/models/illustrious-v1.safetensors \
  s3://WORKER-ARTIFACT-BUCKET/worker/checkpoints/illustrious-v1.safetensors \
  --content-type application/octet-stream \
  --metadata "sha256=${artifact_sha256}" \
  --no-progress
```

The onboarding command refuses a missing object, a size mismatch, or absent or
different `sha256` metadata. The GPU worker independently downloads and hashes
the complete object before ComfyUI starts.

## Anima family onboarding

The pinned artifact catalog retains all approved static families. Its Anima
entries use these roles:

- one or more `diffusion_model` entries for the selectable primary model;
- exactly one `text_encoder` and one `vae` support entry; and
- zero or more `lora` entries whose approvals also declare
  `"model_family": "anima"`.

The onboarding service maps a `diffusion_model` to the existing semantic
`checkpoint` approval kind. This preserves the release contract: the user
selects one primary model regardless of whether ComfyUI loads it with
`CheckpointLoaderSimple` or `UNETLoader`. Text encoders, VAEs, and detectors
are not release-selectable. They therefore have no model approval block or
registry row; their exact key, VersionId, size, SHA-256, target filename, and
manifest trust anchor still fail closed at worker bootstrap.

Use the exact-key namespaces `worker/diffusion-models/`,
`worker/text-encoders/`, and `worker/vae/`. Add every resulting immutable
VersionId to `salad_worker_artifact_object_versions`, just as for existing
checkpoint and LoRA objects. Append Anima's primary model, text encoder, VAE,
and LoRAs to the pinned catalog by setting `base_manifest_path` to an exact
read-only copy of the currently deployed catalog. The generated catalog must
retain the Illustrious entries. At job admission, the controller derives a
demand-scoped runtime manifest containing only the selected primary model,
selected family-compatible LoRAs, and that family's required support files.
Consequently, catalog membership does not cause an Illustrious replica to
download unused Anima bytes or pay their cold-start egress cost.

[`examples/anima-artifact-onboarding-plan.template.json`](../examples/anima-artifact-onboarding-plan.template.json)
pins the researched MiaoMiao Anima Base, Qwen text encoder, Qwen image VAE,
three Anima LoRAs, and bundled Anima workflow by exact SHA-256 and byte size.
Its `base_manifest_path` deliberately points at
`../catalog/current-deployed-artifact-manifest.json`; replace or mount that
file with the exact catalog currently pinned in deployment configuration. The
command refuses a missing or invalid base manifest, a retained artifact whose
pinned remote version is unavailable or has mismatched metadata, and any new
entry that conflicts with the retained catalog. Before running, compare the
base file's `manifest_sha256` with the separately deployed
`GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_SHA256` trust anchor; abort the
handoff if they differ.
Its selectable artifacts are truthfully marked
`"commercial_use_approved": false` and `"experiment_only": true`. Stock
Anima's license permits internal non-production testing and evaluation; those
approvals do not authorize the normal production queue, public hosting, or
commercial generation. Copy the template, independently validate the mounted
Safetensors, review the version-specific evidence, and keep it on the bounded
experiment path. Changing `experiment_only` to false requires separate,
documented rights that permit the intended deployment; never change the flag
merely to bypass the route gate.

Every selectable artifact approval and workflow entry carries
`model_family`. Existing plans default to `illustrious` for compatibility, but
operator plans should set the field explicitly. A workflow family must match a
primary model family in the same plan. Exact Civitai model-version URLs are
accepted only in canonical `https://civitai.com/models/<id>?modelVersionId=<id>`
form; arbitrary query strings remain rejected.

## Edit and apply the plan

Copy `examples/artifact-onboarding-plan.json`, replace its model paths, object
keys, source/license evidence, explicit model-family fields, and optional owner
username, then run. That generic example sets `base_manifest_path` to `null`
because it describes an initial bootstrap; replace it with the deployed
catalog path for every update:

```bash
python -m gen_automation.artifact_onboarding_cli \
  /config/artifact-onboarding-plan.json \
  --manifest-out /restricted-output/artifact-manifest.json
```

The equivalent container invocation mounts every input read-only and only the
output directory writable:

```bash
docker run --rm \
  --env-file /restricted/config/artifact-onboarding.env \
  --mount type=bind,src="$PWD/examples",dst=/config,readonly \
  --mount type=bind,src="$PWD/models",dst=/models,readonly \
  --mount type=bind,src="$PWD/workflows",dst=/workflows,readonly \
  --mount type=bind,src="$PWD/catalog",dst=/catalog,readonly \
  --mount type=bind,src="$PWD/restricted-output",dst=/restricted-output \
  ghcr.io/neuraln-cyber/gen-automation/control-plane:<immutable-tag> \
  python -m gen_automation.artifact_onboarding_cli \
  /config/artifact-onboarding-plan.json \
  --manifest-out /restricted-output/artifact-manifest.json
```

For that container layout, use `/models/...`, `/workflows/...`, and
`/catalog/current-deployed-artifact-manifest.json` local paths in the mounted
copy of the plan.

Paths in the plan are relative to the plan file unless absolute. A local model
path is recommended because it lets this command independently hash and
format-check the bytes. For an object that is already uploaded and not mounted,
remove `local_path` and set both `sha256` and `exact_size_bytes`; the exact
remote object metadata is still verified.

Omit `base_manifest_path` only when bootstrapping the first catalog. Every
additive update must use the exact currently deployed catalog as its base. The
CLI protects that file like every other input: neither `--manifest-out` nor
`--sha256-out` may overwrite it. An unchanged new entry already present in the
base is deduplicated; any changed identity, name, source key, VersionId, or
runtime target fails closed.

The default trust-anchor sidecar is
`artifact-manifest.json.sha256`. Put the JSON file's contents into
`GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_JSON` and the sidecar value into
`GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_SHA256` through the deployment
secret/config mechanism, not chat or command-line arguments.

Rerunning an unchanged plan is safe: exact artifact versions and immutable,
content-addressed workflow objects are verified, registry
commands replay their deterministic idempotency records, and the same manifest
and SHA-256 are emitted. Editing model bytes requires a new object version (a
new content-addressed key is still recommended) and a fresh run. Editing a
workflow requires a new object key containing the new full SHA-256.

Primary-model and LoRA approvals retain explicit usage-scope, adult-use,
license, evidence, Safetensors, and model-family assertions. Production
approvals require `commercial_use_approved`; a non-commercial approval must be
`experiment_only` and is rejected by normal production submission. The
detector, text encoder, and VAE are not release-selectable models and therefore
have no model-registry rows; their exact source keys, formats, byte sizes,
SHA-256 values, and separately deployed manifest trust anchor are enforced at
worker startup.
