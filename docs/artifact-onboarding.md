# Artifact and workflow onboarding

`gen-automation-artifacts` is the one-off operator path for checkpoints,
LoRAs, the optional face detector, and the bundled ComfyUI workflows. Adding
an artifact is an edit-and-rerun operation; no database row or worker manifest
is written by hand.

The command:

1. resolves the one active owner, or the `owner_username` named in the plan;
2. hashes and format-checks each mounted `.safetensors` or detector `.pt` file
   with the same validators used during GPU-worker bootstrap;
3. verifies that the exact worker-artifact object key already has the same byte
   size and `sha256` object metadata, then pins its non-null object VersionId in
   the worker manifest;
4. parses each workflow with duplicate-key, depth, item-count, and size bounds,
   then rejects every node class outside the worker allowlist;
5. requires the workflow SHA-256 in its content-addressed object key, uploads
   it with a create-only write, or verifies the existing object byte-for-byte;
6. creates or reaffirms the current checkpoint, LoRA, and workflow compliance
   approvals through the existing audited registry service; and
7. writes canonical `ArtifactManifest` JSON plus its separate SHA-256 trust
   anchor.

It never proxies a multi-gigabyte model through FastAPI. Checkpoints, LoRAs,
and detector files cross the large-file boundary directly into the private
worker artifact bucket. This keeps memory use and cloud egress predictable.

## Prerequisites

- Run all database migrations and create an active owner account.
- Enable object versioning on private workflow storage. Runtime workflow reads
  require an exact version and ETag.
- Give the one-off job read/write access to workflow storage and read access to
  the worker artifact bucket.
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

## Edit and apply the plan

Copy
`examples/artifact-onboarding-plan.json`, replace its model paths, object keys,
source/license evidence, and optional owner username, then run:

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
  --mount type=bind,src="$PWD/restricted-output",dst=/restricted-output \
  ghcr.io/neuraln-cyber/gen-automation/control-plane:<immutable-tag> \
  python -m gen_automation.artifact_onboarding_cli \
  /config/artifact-onboarding-plan.json \
  --manifest-out /restricted-output/artifact-manifest.json
```

For that container layout, use `/models/...` and `/workflows/...` local paths
in the mounted copy of the plan.

Paths in the plan are relative to the plan file unless absolute. A local model
path is recommended because it lets this command independently hash and
format-check the bytes. For an object that is already uploaded and not mounted,
remove `local_path` and set both `sha256` and `exact_size_bytes`; the exact
remote object metadata is still verified.

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

Checkpoint and LoRA approvals retain the existing explicit commercial-use,
adult-use, license, evidence, and Safetensors assertions. The detector is not a
release-selectable model and therefore has no model-registry row; its exact
source key, archive format, byte size, SHA-256, and separately deployed
manifest trust anchor are enforced at worker startup.
