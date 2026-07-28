# Prompt wildcard libraries

Operators can manage Forge/Stable-Diffusion-style prompt wildcards without
editing a GPU-worker filesystem. Open `/dashboard/wildcards`, paste one entry
per line from an existing wildcard text file, and give the library a name such
as `poses` or `positions/sitting`.

Use the library in either release prompt with double-underscore syntax:

```text
portrait, __poses__, __positions/sitting__
```

Wildcard entries may contain other wildcard tokens. The service rejects a
release if a referenced library is missing, contains a dependency cycle, or
exceeds the configured library, entry, expansion, prompt-length, or nesting
limits.

## Version and reproducibility contract

Creating, replacing, or appending entries always preserves the prior version.
When a release is created, the control plane resolves every direct and nested
token and freezes the exact library/version identifiers, entry counts, and
canonical SHA-256 digests into that release specification. Later edits affect
only future releases.

At generation-plan approval, each job:

1. reloads only the versions frozen by the release;
2. verifies their entry counts and SHA-256 digests;
3. selects entries deterministically from the job seed, prompt field,
   occurrence number, and frozen version identity;
4. expands nested tokens with bounded depth and total substitutions; and
5. stores the resolved positive and negative prompts plus version, selection,
   entry-index, and digest evidence in `GenerationJob.parameters`.

The GPU worker therefore receives the fully resolved prompt. It does not read
mutable wildcard files and cannot silently change the meaning of an already
approved release.

Wildcard selection happens once per generation job. For a different wildcard
draw on every generated image, configure the plan with `outputs_per_job=1`;
multiple outputs from one job intentionally share that job's resolved prompt.

## API

All endpoints require an authenticated operator. Mutations require the
`manage_releases` permission and the normal same-origin/CSRF boundary.

```text
GET  /api/v1/wildcards
GET  /api/v1/wildcards/{name}
POST /api/v1/wildcards
PUT  /api/v1/wildcards/{name}
POST /api/v1/wildcards/{name}/entries
```

Replace and append commands include `expected_version_no`; a stale editor gets
a conflict instead of overwriting somebody else's newer version.

Example create body:

```json
{
  "name": "poses",
  "entries": [
    "standing, hands on hips",
    "sitting on a chair"
  ]
}
```

Example append body:

```json
{
  "expected_version_no": 1,
  "entries": [
    "kneeling"
  ]
}
```
