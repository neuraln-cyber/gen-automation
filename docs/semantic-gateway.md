# Private semantic gateway

The semantic gateway is the smallest deployable boundary between the control
plane's `semantic-anatomy-assessment/v1` contract and a private,
OpenAI-compatible vision model server such as vLLM. It does not contain model
weights, start a GPU provider, or grant access to one.

It performs four fail-closed checks before inference:

1. bounds the JSON request and decoded JPEG, PNG, or WebP bytes;
2. verifies the exact image digest, deterministic request ID, idempotency key,
   fixed prompt, fixed schema, and both fixed hashes;
3. requires the request's model and immutable revision to equal this
   deployment's configuration; and
4. requires the declared JPEG/PNG/WebP MIME to match the decoded format,
   rejects rasters above 8,388,608 pixels, applies EXIF orientation, preserves
   correctly oriented images at or below a 1536-pixel long edge, and serializes
   larger-image resizing to a temporary PNG analysis copy; and
5. asks the upstream model for strict JSON-schema output, validates the result,
   and returns the identity-bound envelope expected by `SemanticVlmClient`.

The resize keeps large Illustrious masters within Qwen3-VL's visual-token
budget. It never changes the stored master. The prompt includes the
normalization version and geometry, so its digest—and therefore the assessment
profile—changes whenever this preprocessing contract changes.

Invalid input receives a bounded 4xx response. Transport failures and transient
upstream failures receive 503; malformed or unexpected upstream responses
receive 502. The service does not log request bodies, images, base64, or
credentials. Run Uvicorn with access logging disabled as shown below.

## Run

Configure the gateway:

```text
GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_CHAT_COMPLETIONS_URL=http://vllm:8000/v1/chat/completions
GEN_AUTOMATION_SEMANTIC_GATEWAY_MODEL=Qwen/Qwen3-VL-8B-Instruct
GEN_AUTOMATION_SEMANTIC_GATEWAY_MODEL_REVISION=<immutable model commit or release>
GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_TIMEOUT_SECONDS=120
GEN_AUTOMATION_SEMANTIC_GATEWAY_MAX_IMAGE_BYTES=20971520
```

Set `GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_API_KEY` only when the private
model server requires it. Do not put a credential in the upstream URL.

Run from the repository:

```text
python -m uvicorn gen_automation.semantic_gateway.app:create_app --factory --host 0.0.0.0 --port 8080 --no-proxy-headers --no-access-log --no-server-header --no-date-header
```

Or build `Dockerfile.semantic-gateway`. Point the control plane's
`GEN_AUTOMATION_SEMANTIC_ANATOMY_ENDPOINT_URL` at
`http://<private-gateway>:8080/v1/anatomy/assess` and configure the same exact
model and revision on both sides.

## Pinned vLLM contract

The gateway sends standard `POST /v1/chat/completions` input with:

- `model` equal to the configured model identifier;
- a data-URL image and the fixed anatomy prompt;
- deterministic decoding (`temperature=0`, `seed=0`);
- `response_format.type=json_schema`, `strict=true`; and
- an identity-bound copy of the repository's fixed output schema.

The OpenAI-compatible protocol has no standard model-revision field. The
gateway therefore includes the configured revision in
`X-Gen-Automation-Model-Revision`, and the model server deployment must load the
same immutable revision. For vLLM, use the equivalent of:

```text
vllm serve "$MODEL" --revision "$MODEL_REVISION" --served-model-name "$MODEL"
```

The gateway verifies configuration and wire identities; it cannot prove which
weights an independently operated upstream loaded. Pin that in the deployment
manifest and image/launch configuration.

## Scale-to-zero deployment contract

- Keep the gateway and model endpoint on private networking. The current
  controller contract intentionally has no bearer token; use private ingress or
  an authenticated service-mesh/ingress boundary if traffic leaves that network.
- Set minimum GPU replicas to zero, maximum replicas to the desired cost cap,
  and concurrency to one per loaded model unless measured capacity supports
  more. Queue requests during cold start rather than returning a synthetic pass.
- Give provider cold-start plus inference less time than both the gateway
  upstream timeout and the controller semantic timeout. A 503 is safe: the
  controller already retries boundedly and ultimately leaves the image for
  manual review.
- Mount a provider-managed model cache or volume if its storage cost is lower
  than repeated model downloads. The gateway image never packages model
  weights.
- `/health/live` proves the gateway process is alive. `/health/ready` proves its
  configuration loaded; co-located deployments should additionally gate
  external readiness on the model server's own readiness probe.
- Requests are deterministic and keyed by `Idempotency-Key`, but the gateway
  stores no result cache. Provider queues may safely retry the same request.

AWS staging uses five total controller attempts with backoff bounded at 30,
60, 120, and 120 seconds. This tolerates a scale-to-zero cold start while
keeping latency and spend finite. The cap limits assessment rows rather than
provider calls, so one row can still produce as many as five billable requests.
Existing terminal rows are immutable and are never reopened by changing the
retry configuration.

No cloud account, GPU allocation, model-download credential, or provider API
key is needed to build and test this gateway. Those are live deployment inputs.
