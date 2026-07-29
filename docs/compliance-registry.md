# Compliance registry

Generation planning fails closed unless the current release specification matches
current server-owned approvals for every subject, checkpoint, LoRA, and workflow.
The registry records an evidence digest and the administrator who made each
decision; evidence documents themselves remain in the operator's controlled
legal-record system.

## Operator contract

- Only active `OWNER` and `ADMIN` users can mutate the registry.
- Mutations require a recently authenticated session, same-origin request,
  cookie-bound CSRF token, and an `Idempotency-Key`.
- A subject must be fictional, canonically at least 18, unmistakably adult, not
  an aged-up minor, and separately approved for commercial distribution and
  adult derivative use.
- A checkpoint or LoRA must have commercial/adult-use approval and a verified
  Safetensors artifact.
- A workflow approval freezes its SHA-256, object key, version, and reviewed node
  classes.
- Reapproval creates a new version and revokes the superseded current version.
  Repeating an identical command is idempotent and does not create another row.
- Revocation immediately removes the record from current approval lookup. It does
  not delete history or evidence digests.

The mutation endpoints are:

```text
POST /api/v1/compliance/subjects
POST /api/v1/compliance/model-artifacts
POST /api/v1/compliance/workflows
POST /api/v1/compliance/subjects/{approval_id}:revoke
POST /api/v1/compliance/model-artifacts/{approval_id}:revoke
POST /api/v1/compliance/workflows/{approval_id}:revoke
```

Current approvals can be inspected by an owner or administrator at:

```text
GET /api/v1/compliance/subject
GET /api/v1/compliance/model_artifact
GET /api/v1/compliance/workflow
```

Do not paste contracts, access tokens, private evidence, or credentials into
chat, release prompts, logs, or Git. Store evidence documents in the designated
private legal-record system and register only bounded references and SHA-256
digests.
