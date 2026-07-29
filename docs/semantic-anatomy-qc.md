# Optional semantic anatomy QC

Semantic anatomy QC is a second, optional review signal after deterministic CPU
quality ranking. It is designed for a private burst or scale-to-zero GPU service;
the normal control plane and human review do not require a second always-on GPU.

The stage looks only for these bounded issue codes:

- `extra_finger`, `missing_finger`, `malformed_hand`
- `extra_toe`, `missing_toe`, `malformed_foot`
- `extra_limb`, `missing_limb`, `duplicate_body_part`
- `impossible_joint`, `implausible_proportion`
- `severe_face_deformation`

It records `pass`, `review`, or `severe`, normalized confidence, optional
normalized boxes, the pinned model/revision, and SHA-256 digests of the exact
assessment prompt and output schema. A completed or unavailable assessment is
immutable. It never edits, rejects, quarantines, or deletes the raw master.

## Review behavior

Only a `severe` result at or above
`GEN_AUTOMATION_SEMANTIC_ANATOMY_SEVERE_CONFIDENCE_MICROS` enters the clearly
labeled **AI excluded** section at the bottom of the review page. The image stays
visible with the same Accept, Reject, Hold, download, and X-selection controls.
A human decision is authoritative.

`review`, low-confidence `severe`, pending, and retrying results remain in normal
rank order. If the service is unavailable or returns an invalid contract, the
assessment retries with bounded backoff and eventually displays “unavailable;
review manually.” There is no synthetic pass and review completion is not
blocked.

## Private service contract

The controller sends `POST` to the exact configured endpoint with JSON:

```json
{
  "schema_version": "semantic-anatomy-assessment/v1",
  "request_id": "<deterministic sha256>",
  "model": "<configured model>",
  "model_revision": "<pinned revision>",
  "image": {
    "content_type": "image/png",
    "sha256": "<raw-master sha256>",
    "base64": "<exact version bytes>"
  },
  "task": {
    "prompt": "<fixed anatomy prompt>",
    "prompt_sha256": "<sha256>",
    "output_schema": {},
    "schema_sha256": "<sha256>"
  }
}
```

The gateway returns HTTP 200 and `application/json`:

```json
{
  "schema_version": "semantic-anatomy-assessment/v1",
  "request_id": "<same request id>",
  "model": "<same model>",
  "model_revision": "<same revision>",
  "asset_sha256": "<same raw-master sha256>",
  "assessment": {
    "verdict": "severe",
    "confidence": 0.96,
    "issues": [
      {
        "code": "extra_finger",
        "confidence": 0.98,
        "box": {"x_min": 0.1, "y_min": 0.2, "x_max": 0.4, "y_max": 0.7}
      }
    ]
  }
}
```

Unknown fields, issue codes, identity mismatches, malformed boxes, oversized
responses, redirects, and non-JSON responses fail closed as unavailable review
signals. The gateway may wrap a pinned Qwen3-VL-class model, but the model and
revision are configuration rather than a hard dependency.

## Activation and live requirements

Leave `GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=false` until the service exists.
Source-side tests use a fake HTTP transport and need no account, key, or GPU.

Live activation requires:

1. a private burst/scale-to-zero endpoint implementing the contract above;
2. an exact model identifier and immutable model revision;
3. network access from the control plane to that endpoint; and
4. the existing PostgreSQL and exact-version object-store access used by CPU QC.

Set:

```text
GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=true
GEN_AUTOMATION_SEMANTIC_ANATOMY_ENDPOINT_URL=https://<private-service>/v1/anatomy/assess
GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL=Qwen/Qwen3-VL-8B-Instruct
GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL_REVISION=<immutable revision>
```

No credential is required by the application contract. Put the endpoint on a
private network or add authentication at the private gateway/ingress boundary
before exposing it outside that network.
