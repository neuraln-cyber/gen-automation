# SaladCloud integration contract

Official documentation is the source of truth for this adapter:

- API usage: <https://docs.salad.com/reference/api-usage>
- ComfyUI deployment:
  <https://docs.salad.com/container-engine/how-to-guides/ai-machine-learning/deploy-stable-diffusion-comfy>
- Job queues:
  <https://docs.salad.com/container-engine/explanation/job-processing/job-queues>
- Webhook verification:
  <https://docs.salad.com/container-engine/how-to-guides/job-processing/webhook-signature>

## Authentication

```text
Base URL: https://api.salad.com/api/public
Header:   Salad-Api-Key: <token>
```

Salad documents API keys as per-user tokens covering resources available to that
user rather than narrowly scoped service tokens. A dedicated Salad automation
user is therefore required. The token exists only in the control-plane secret
store.

Required settings:

```text
SALAD_API_KEY
SALAD_ORGANIZATION
SALAD_PROJECT
SALAD_QUEUE_NAME
SALAD_CONTAINER_GROUP_NAME
SALAD_WEBHOOK_SECRET
SALAD_WORKER_IMAGE
```

API callers must tolerate rate limits and non-JSON error responses.

## Provisioning

1. Fetch organization quotas and current GPU classes/availability before deployment.
2. Pin the accepted 24 GB GPU class UUIDs in
   `GEN_AUTOMATION_SALAD_GPU_CLASS_IDS`; this makes each deployment version
   reproducible while allowing the selection to be refreshed when availability
   or pricing changes.
3. Push the worker image using an immutable registry digest.
4. Start the controller with both `GEN_AUTOMATION_SALAD_ENABLED=true` and
   `GEN_AUTOMATION_GPU_ALLOCATION_ENABLED=true`.
5. Let the normal deployment reconciliation loop create the versioned queue and
   queue-connected container group through the API.
6. Run a paid one-job canary before enabling normal traffic.

Relevant operations:

```text
GET  /organizations/{org}/quotas
GET  /organizations/{org}/gpu-classes
POST /organizations/{org}/availability/sce-gpu-availability

POST /organizations/{org}/projects/{project}/queues
POST /organizations/{org}/projects/{project}/containers
```

Queue connection is treated as immutable. A deployment that changes it creates a
new versioned queue/group rather than mutating production in place.

### Automatic deployment bootstrap

On controller startup, a fully configured database automatically gets one
current immutable `SaladDeployment` intent. Startup itself performs no provider
API request. An identical restart reuses the same row; changing the worker
digest, provider names, GPU classes, resource sizing, runtime-binding set, queue
limit, replica limit, or hourly cost ceiling creates the next version and marks
the previous version for stop. The existing deployment reconciliation loop
performs all remote creates and stops.

No deployment intent is created while GPU allocation is disabled. Disabling it
also retains the existing fail-closed behavior that marks active deployment
intent for stop.

The non-secret provider inputs are:

| Setting | Default / constraint |
| --- | --- |
| `GEN_AUTOMATION_SALAD_GPU_CLASS_IDS` | Required JSON array when allocation is enabled; 1–16 unique Salad GPU class UUIDs. |
| `GEN_AUTOMATION_SALAD_CONTAINER_CPU` | `4`; 1–16 vCPU. |
| `GEN_AUTOMATION_SALAD_CONTAINER_MEMORY_MB` | `16384`; 1024–65536 MiB. |
| `GEN_AUTOMATION_SALAD_CONTAINER_STORAGE_BYTES` | `53687091200` (50 GiB); 10–250 GiB. |
| `GEN_AUTOMATION_SALAD_MAX_HOURLY_COST_USD` | `1.00`; positive, at most the daily budget, with micro-dollar precision. Configure it at or above the highest selected GPU rate because durable reservations and spend accounting use this ceiling. |

Low priority and image caching are fixed for the MVP. The initial replica count
and autoscaler minimum are fixed at zero; the validated maximum replica and
queue-length settings remain capped at one.

## Container group

The initial production posture is:

- `replicas: 0`
- queue autoscaler minimum `0`, maximum `1`
- desired queue length `1`
- queue polling every `15` seconds (the provider minimum)
- low priority
- one or more recently discovered and configuration-pinned 24 GB GPU classes
- image caching enabled
- startup/liveness probe `GET /health` on port `8000`
- readiness probe `GET /ready` on port `8000`
- queue HTTP target `POST /jobs/generate` on port `8000`
- no Container Gateway for the queue-only worker

Normal idle cost control uses queue autoscaling to zero. Explicit stop is for
maintenance or the global kill switch because stop destroys runtime instances
and their local data.

The worker image pins ComfyUI and embeds the pinned Salad HTTP Job Queue worker.
The runtime supervises ComfyUI, the embedded queue forwarder, and the worker HTTP
API as one failure domain. The Salad container-group queue connection must
forward each queue input to the worker API:

```json
{
  "queue_connection": {
    "path": "/jobs/generate",
    "port": 8000
  }
}
```

The ComfyUI API remains loopback-only at `http://127.0.0.1:8188`; neither it nor
ComfyUI Manager is exposed through a Container Gateway.

Expected paths:

```text
/opt/comfyui/main.py
/opt/comfyui/models/checkpoints
/opt/comfyui/models/loras
/opt/comfyui/custom_nodes
/opt/worker/runtime
```

The model directories and runtime directory are writable by the non-root worker.
The pinned ComfyUI source remains in `/opt/comfyui`. Model bootstrap completes
before the HTTP server starts, `GET /health` reports process liveness, and
`GET /ready` returns success only when the loopback ComfyUI executor is ready.

## GPU worker runtime settings

`WorkerRuntimeSettings` reads only the `GEN_WORKER_` prefix. JSON-valued settings
must be supplied as valid JSON, not shell or Python syntax. The exhaustive
deployment template is in `.env.example`; its secret fields intentionally remain
blank.

Required staging values:

| Setting | Contract |
| --- | --- |
| `GEN_WORKER_ENVIRONMENT` | `production` in staging/production; relaxations are test-only. |
| `GEN_WORKER_VERIFICATION_KEYS` | JSON object of key ID to an unpadded base64url Ed25519 public key. Keep only the active rotation set. The controller private key must never enter the worker container. |
| `GEN_WORKER_ALLOWED_UPLOAD_ORIGIN` | Exact HTTPS origin, with no path/query, used to validate controller-issued output upload grants. |
| `GEN_WORKER_MODEL_MANIFEST_JSON` | Canonically ordered v1 manifest containing opaque S3 object IDs, exact sizes, filenames, and SHA-256 values for every checkpoint/LoRA. |
| `GEN_WORKER_MODEL_MANIFEST_SHA256` | Separately approved 64-character digest injected as an independent trust anchor. It must not be derived from the inline manifest by the worker at startup. |
| `GEN_WORKER_ARTIFACT_BUCKET` | Private S3-compatible model-artifact bucket. It is separate from the asset archive permission boundary. |

Artifact-source settings:

| Setting | Default or rule |
| --- | --- |
| `GEN_WORKER_ARTIFACT_REGION` | `us-east-1`. |
| `GEN_WORKER_ARTIFACT_ENDPOINT_URL` | Provider HTTPS S3 endpoint; production rejects an HTTP endpoint. Omit for AWS default resolution. |
| `GEN_WORKER_ARTIFACT_ACCESS_KEY_ID`, `GEN_WORKER_ARTIFACT_SECRET_ACCESS_KEY`, `GEN_WORKER_ARTIFACT_SESSION_TOKEN` | Required as one indivisible short-lived set in a production Salad worker. In AWS, the controller mints these from `GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_ROLE_ARN`; configured static keys and a custom endpoint are rejected in role mode. |
| `GEN_WORKER_ARTIFACT_CONNECT_TIMEOUT_SECONDS`, `GEN_WORKER_ARTIFACT_READ_TIMEOUT_SECONDS` | `10` and `120`; bounded S3 bootstrap network timeouts. |
| `GEN_WORKER_MODEL_BOOTSTRAP_TIMEOUT_SECONDS` | `3600`; hard wall-time cap for the complete verified model bootstrap so a stalled cold start cannot spend indefinitely. |

The artifact identity is read-only: it may `GetObject` only the approved model
prefix/object IDs and may not list, write, delete, or read the asset archive.
Every object is streamed under exact-size and SHA-256 checks. Bootstrap then
removes explicit and ambient AWS credential variables before starting ComfyUI,
and the ComfyUI child receives an allowlisted environment. Output images use
controller-issued, exact-origin presigned upload grants instead of the artifact
identity.

The controller uses the same pinned manifest before submitting a queue job.
Release checkpoints and LoRAs must match its kind, SHA-256, and S3 object ID;
Comfy loader inputs are then rendered from the manifest `target_filename`.
Rendered loader names and LoRA weights are checked before upload grants or a
provider mutation are created. See `generation-workflows.md`.

Runtime paths and processes:

| Setting | Default |
| --- | --- |
| `GEN_WORKER_CHECKPOINT_ROOT`, `GEN_WORKER_LORA_ROOT` | `/opt/comfyui/models/checkpoints`, `/opt/comfyui/models/loras`. |
| `GEN_WORKER_COMFY_MAIN`, `GEN_WORKER_COMFY_PYTHON` | `/opt/comfyui/main.py`, `/opt/worker-venv/bin/python`. The dedicated virtual environment reuses the pinned base-image CUDA/PyTorch packages through system site packages while isolating the hash-locked worker and ComfyUI dependencies. |
| `GEN_WORKER_COMFY_BASE_URL` | `http://127.0.0.1:8188`; loopback HTTP only. |
| `GEN_WORKER_COMFY_RUNTIME_ROOT` | `/opt/worker/runtime` for input, output, temp, user data, and the local ComfyUI SQLite database. |
| `GEN_WORKER_COMFY_EXECUTION_TIMEOUT_SECONDS` | `3600`; covers a bounded 25-image graph with face detailing. |
| `GEN_WORKER_SALAD_QUEUE_WORKER_ENABLED` | `true`; required for Salad queue deployments. |
| `GEN_WORKER_SALAD_QUEUE_WORKER_PATH` | `/usr/local/bin/salad-http-job-queue-worker`. |
| `GEN_WORKER_SALAD_QUEUE_WORKER_LOG_LEVEL` | `error`. |
| `GEN_WORKER_WORKER_HOST`, `GEN_WORKER_WORKER_PORT` | `0.0.0.0`, `8000`; the host is intentionally restricted to all-interface container binds. |
| `GEN_WORKER_WORKER_LOG_LEVEL` | `info`. |

Security/resource limits:

| Settings | Purpose |
| --- | --- |
| `GEN_WORKER_MAX_BODY_BYTES`, `GEN_WORKER_MAX_SIGNATURE_TTL_SECONDS`, `GEN_WORKER_CLOCK_SKEW_SECONDS` | Bound signed queue requests and authorization time windows. |
| `GEN_WORKER_MAX_OUTPUTS`, `GEN_WORKER_MAX_OUTPUT_BYTES`, `GEN_WORKER_MAX_TOTAL_OUTPUT_BYTES` | Bound output count, individual bytes, and aggregate bytes. The worker defaults to 25 outputs per provider job and retains a hard ceiling of 32. |
| `GEN_WORKER_MAX_IMAGE_DIMENSION`, `GEN_WORKER_MAX_IMAGE_PIXELS` | Bound decoded output geometry. |
| `GEN_WORKER_MAX_REPLAY_ENTRIES` | Bounds the in-process duplicate receipt cache. |
| `GEN_WORKER_UPLOAD_TIMEOUT_SECONDS`, `GEN_WORKER_READINESS_TIMEOUT_SECONDS` | Bound presigned uploads and readiness checks. |
| `GEN_WORKER_APPROVED_WORKFLOW_NODE_CLASSES` | JSON array of the only ComfyUI node classes accepted by the worker; it must include an approved save node. |

The current defaults are recorded in `.env.example`. Tightening them is a
deployment change; expanding them requires a security and cost review.

No GPU-worker credential is required during the present local development
stage. Create the dedicated model-read identity, controller signing key and worker
public-key set, manifest
secret, and Salad runtime bindings only for staging deployment. Runtime bindings
store secret references, never live values, in the durable deployment
configuration; the resolver supplies values only while creating the Salad
container group.

### Runtime secret resolver

The control plane reads worker bootstrap values from deployment-secret-store
injected `GEN_AUTOMATION_SALAD_WORKER_*` settings. Every value is held as a
Pydantic `SecretStr`; resolved raw mappings are not placed in deployment rows,
audit events, application state, exception text, or logs. The resolver itself
is not exposed on application state and is closed only after the controller
runtime has stopped.

The durable `provider_configuration.runtime_bindings` array contains only an
approved target and its fixed, non-secret alias. Staging and production GPU
allocation fails configuration validation unless every required controller-side
value is present. Provisioning then fails before the container-group POST unless
the durable binding set exactly matches the configured required set. Local and
test environments need no worker credentials.

Required protected-environment bindings:

```json
[
  {"name":"GEN_WORKER_ENVIRONMENT","reference":"deployment-config://salad-worker/environment"},
  {"name":"GEN_WORKER_VERIFICATION_KEYS","reference":"deployment-config://salad-worker/verification-keys"},
  {"name":"GEN_WORKER_ALLOWED_UPLOAD_ORIGIN","reference":"deployment-config://salad-worker/allowed-upload-origin"},
  {"name":"GEN_WORKER_MODEL_MANIFEST_JSON","reference":"deployment-config://salad-worker/model-manifest-json"},
  {"name":"GEN_WORKER_MODEL_MANIFEST_SHA256","reference":"deployment-config://salad-worker/model-manifest-sha256"},
  {"name":"GEN_WORKER_ARTIFACT_BUCKET","reference":"deployment-config://salad-worker/artifact-bucket"},
  {"name":"GEN_WORKER_ARTIFACT_REGION","reference":"deployment-config://salad-worker/artifact-region"},
  {"name":"GEN_WORKER_ARTIFACT_ACCESS_KEY_ID","reference":"deployment-config://salad-worker/artifact-access-key-id"},
  {"name":"GEN_WORKER_ARTIFACT_SECRET_ACCESS_KEY","reference":"deployment-config://salad-worker/artifact-secret-access-key"}
]
```

`GEN_WORKER_ENVIRONMENT=production` and the public verification-key JSON are
derived by the controller; the signing private key is never resolved into the
worker. `GEN_WORKER_ARTIFACT_ENDPOINT_URL` and
`GEN_WORKER_ARTIFACT_SESSION_TOKEN` have fixed aliases in the same allowlist and
must be included in the durable binding set when their corresponding
controller-side settings are configured. Any other target name or alias is
rejected.

## Job submission

```text
POST /organizations/{org}/projects/{project}/queues/{queue}/jobs
```

Conceptual payload:

```json
{
  "input": {
    "input": {
      "internal_job_id": "controller UUID",
      "release_version_id": "controller UUID",
      "generation_specification": {}
    },
    "uploads": [
      {
        "output_index": 0,
        "put_url": "short-lived predetermined URL",
        "object_key": "staging/job-id/0"
      }
    ]
  },
  "metadata": {
    "internal_job_id": "controller UUID"
  },
  "webhook": "https://hooks.example.com/salad"
}
```

The actual signed custom-workflow envelope is covered by a contract test against
the pinned `POST http://<worker>:8000/jobs/generate` worker API. Only small JSON
metadata returns through the queue; image bytes upload directly to predetermined
object-store keys.

Reconciliation operations:

```text
GET    .../queues/{queue}/jobs/{provider_job_id}
GET    .../queues/{queue}/jobs
DELETE .../queues/{queue}/jobs/{provider_job_id}
```

Local states map Salad's `pending`, `running`, `succeeded`, `cancelled`, and
`failed` values into the richer local state machine.

## Webhooks

The raw request is verified with:

```text
webhook-signature
webhook-id
webhook-timestamp
```

The organization webhook secret is retrieved during provisioning and stored as a
secret. The webhook ID is persisted with a uniqueness constraint before
processing. Polling reconciliation remains mandatory because documented webhook
delivery guarantees are not sufficient to make callbacks the only source of
truth.

## Adapter uncertainties

- Salad's job-creation API does not document a client idempotency key; the
  controller outbox and deterministic storage keys provide local deduplication.
- The exact accepted container resource payload is validated using the Portal's
  copied configuration and a live canary.
- Queue-worker behavior cannot be completely reproduced off-platform.
- Scaling from zero can be slow because ephemeral nodes may download models
  again; jobs remain queued during cold start.
- GPU UUIDs, availability, pricing, and published base-image versions are
  operator-verified configuration, not source-code constants.
