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

Wildcard selection happens once per planned output. A job with
`outputs_per_job=4` stores four independently resolved prompt variants and the
worker renders them as four `batch_size=1` branches inside one provider job.
This preserves one queue submission while giving every image its own wildcard
draw and deterministic seed.

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

## One-off text-file import

For an initial migration or a later bulk refresh, use the bounded operator CLI
instead of bypassing the authenticated web routes. The CLI needs only the normal
database secret identity and resolves the named, active owner so every created
or replaced library version uses the existing audit trail.

Create a non-secret plan with an explicit source-to-library mapping:

```json
{
  "version": "v1",
  "owner_username": "owner@example.test",
  "libraries": [
    {
      "source_path": "wildcards/poses.txt",
      "library_name": "poses"
    },
    {
      "source_path": "wildcards/positions/sitting.txt",
      "library_name": "positions/sitting"
    }
  ]
}
```

Paths may be absolute or relative to the plan. Each UTF-8 file is interpreted
as one entry per line. Duplicate entries and leading/trailing whitespace are
preserved. Blank or whitespace-only lines are dropped, matching the dashboard
paste behavior; each result reports both the physical source-line count and the
dropped-blank count. The importer rejects missing files and preflights every
source before writing any library.

Run a comparison first:

```text
gen-automation-wildcards wildcard-import.json --dry-run
```

Then apply the same plan:

```text
gen-automation-wildcards wildcard-import.json
```

The command reads `GEN_AUTOMATION_DATABASE_URL` from the environment or `.env`;
the plan must not contain credentials. Creating a library writes version 1,
changed contents write the next immutable version, and an unchanged rerun writes
no version or audit event. Run the command only from the restricted operator
environment that already holds the database identity.
