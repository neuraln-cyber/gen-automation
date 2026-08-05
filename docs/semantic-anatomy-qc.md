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
Before inference, the gateway validates the exact raw-master digest, declared
JPEG/PNG/WebP type, and an 8,388,608-pixel source bound. It applies EXIF
orientation, then resizes images larger than a 1536-pixel long edge without
cropping to a temporary PNG analysis copy. Correctly oriented smaller images
pass through unchanged. Normalization is serialized to bound peak memory. The
source asset is never mutated or replaced, and the versioned normalization
statement is part of the prompt/profile digest so preprocessing changes create
a distinct auditable assessment profile.

## Review behavior

Anatomy assessment has three explicit operating modes:

- `shadow` (the default) records and displays predictions for calibration. It
  never reorders images, prevents a decision, or blocks completion.
- `assist` highlights predictions for the owner while keeping every human
  decision and completion action authoritative.
- `enforce` enables the strict severe-result override and completion gates
  described below. Move to this mode only after owner-labelled calibration is
  ready and its validation metrics are acceptable.

In `enforce` mode only, a `severe` result at or above
`GEN_AUTOMATION_SEMANTIC_ANATOMY_SEVERE_CONFIDENCE_MICROS` enters the clearly
labeled **AI excluded** section at the bottom of the review page. The image stays
visible and the raw master is unchanged. Reject and Hold remain normal reviewer
decisions. Accepting a high-confidence severe result requires an active OWNER,
the exact `semantic_severe_override` reason code, and a written justification;
the resulting profile-bound attestation is stored as a durable audit event.

`review`, low-confidence `severe`, pending, and retrying results remain in normal
rank order. If the service is unavailable or returns an invalid contract, the
assessment retries with bounded backoff and eventually displays “unavailable;
review manually.” There is no synthetic pass. In `enforce` mode, review
completion waits until every ranked image has either a completed assessment or
an explicit terminal `unavailable` result. It also blocks while an accepted
high-confidence severe result lacks the OWNER override attestation.

## Owner feedback and calibration

For each completed assessment, the owner can record one immutable label:
`anatomy_good`, `anatomy_defect`, or `unjudgeable`. Defect labels can include a
bounded issue code and an optional note. The system binds that label to the
exact asset, assessment response, model revision, prompt/schema profile, and
owner. A repeated identical submission is idempotent; a different replacement
is rejected so calibration history cannot be silently rewritten.

Calibration uses explicit anatomy labels plus two narrowly inferred signals from
a completed OWNER review. A final Accept becomes an `anatomy_good` example unless
it overrides a severe anatomy flag. A Reject becomes an `anatomy_defect` example
only when its reason is explicitly anatomy-specific. Generic Reject, Delete,
Exclude, and Hold choices are deliberately not treated as defects because they
can reflect style, composition, duplication, or publishing preferences. An
explicit Good/Defect/Unsure label always wins over an inferred signal.

Threshold reports are deterministic and versioned. Duplicate image content is
kept in one validation fold using the raw-master SHA-256. Five-fold out-of-fold
validation selects thresholds without scoring them on the same examples used to
fit that fold. A candidate becomes the effective severe-confidence threshold only
when it has at least 100 judged examples, including at least 20 good and 20
defective examples, and its held-out F1, defect recall, and false-positive rate do
not regress against the currently applied policy. Otherwise the previous policy
remains active. The dashboard reports the label mix, applied and candidate
versions, validation metrics, and whether learning improved, stayed stable, or
regressed.

This first learning stage is a CPU/SQL personalization layer; it changes the
decision threshold, not the vision model's neural weights, and adds no GPU cost.
A future VLM fine-tune can use the same immutable dataset after it grows large and
balanced enough for a separately validated model revision.

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
signals. The repository includes the private gateway boundary in
`Dockerfile.semantic-gateway`. It translates this controller contract to a
pinned OpenAI-compatible vision-model endpoint. The upstream model and exact
revision remain deployment configuration rather than a source-code dependency.

## Activation and live requirements

Leave `GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=false` until the service exists.
Source-side tests use a fake HTTP transport and need no account, key, or GPU.

Live activation requires:

1. the private semantic gateway plus a private burst/scale-to-zero
   OpenAI-compatible vision-model endpoint;
2. an exact model identifier and immutable model revision;
3. network access from the control plane to that endpoint; and
4. the existing PostgreSQL and exact-version object-store access used by CPU QC.

Set:

```text
GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=true
GEN_AUTOMATION_SEMANTIC_ANATOMY_MODE=shadow
GEN_AUTOMATION_SEMANTIC_ANATOMY_ENDPOINT_URL=https://<private-service>/v1/anatomy/assess
GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL=Qwen/Qwen3-VL-8B-Instruct
GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL_REVISION=<immutable revision>
GEN_AUTOMATION_SEMANTIC_ANATOMY_MAX_ASSESSMENTS_PER_PROFILE=<positive hard limit>
GEN_AUTOMATION_SEMANTIC_ANATOMY_ASSET_ALLOWLIST='["<exact asset UUID>"]'
GEN_AUTOMATION_BACKGROUND_SEMANTIC_MAX_ATTEMPTS=5
GEN_AUTOMATION_BACKGROUND_SEMANTIC_RETRY_BASE_SECONDS=30
GEN_AUTOMATION_BACKGROUND_SEMANTIC_RETRY_MAX_SECONDS=120
```

The default per-profile limit is `0`, so disabled or partially configured anatomy QC
cannot create billable assessments. Enabling the feature requires a positive limit.
Every assessment row for the model/revision profile counts toward that cap, including
completed, unavailable, and retrying rows. When the UUID allowlist is non-empty, only
those exact raw-master assets can receive new assessment rows; already-created rows
may still finish or retry after the allowlist or limit changes.

The staging cold-start policy makes at most five total attempts: the initial
request plus four retries. Retry delays are bounded at 30, 60, 120, and 120
seconds. Every provider call may be billable, so keep the per-profile cap and
exact UUID allowlist small while calibrating. Terminal `completed` and
`unavailable` rows remain immutable; retry settings apply only when a new
assessment row is created.

No credential is required by the application contract. Put the endpoint on a
private network or add authentication at the private gateway/ingress boundary
before exposing it outside that network.
