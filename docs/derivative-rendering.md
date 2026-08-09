# Deterministic derivative rendering

`gen_automation.services.derivatives` is the non-destructive rendering core for
approved raw masters. It has no database, object-storage, filesystem, API, or
publishing side effects. A caller explicitly requests one or both supported
targets, returned in a fixed order:

1. `full`, a provider-neutral full-resolution image downscaled only
   when it exceeds the configured bounds; and
2. `x_teaser`, a destination-specific teaser using `downscale`, `contain`, or
   `cover` fit behavior.

The caller must upload these new bytes under derivative object keys. It must
never overwrite the raw-master object.

## Basic use

```python
from gen_automation.services.derivative_isolation import (
    render_platform_derivatives_isolated,
)
from gen_automation.services.derivatives import (
    BlurCensor,
    DerivativeRecipe,
    RelativeRegion,
    WatermarkSpec,
    XTeaserSpec,
)

recipe = DerivativeRecipe(
    version="release-derivatives-v1",
    watermark=WatermarkSpec(),
    x_teaser=XTeaserSpec(
        censor=BlurCensor(
            region=RelativeRegion(
                x=250_000,
                y=300_000,
                width=500_000,
                height=400_000,
            ),
            radius=18,
        )
    ),
)

bundle = await render_platform_derivatives_isolated(
    verified_raw_master_bytes,
    recipe=recipe,
    watermark_png=transparent_watermark_png_bytes,
    targets=("full", "x_teaser"),
)
```

Production callers must use the async isolated entry point. The synchronous
`render_platform_derivatives` function is the pure deterministic core for unit
tests and explicitly trusted inputs; it is not a production parser boundary.

Relative coordinates are integer millionths of the final teaser canvas. This
avoids float, locale, NaN, and rounding ambiguity in recipes. Recipe JSON is
canonicalized, size-bounded, and hashed. A material recipe change therefore
creates a different derivative identity even if it retains the same human
version label.

## Output contract

Every `RenderedDerivative` contains:

- the immutable output bytes and their SHA-256 digest;
- byte size, image format, content type, extension, and pixel dimensions;
- the safe logical output filename;
- the canonical recipe digest;
- canonical lineage metadata and its digest; and
- source, watermark, renderer, and Pillow version identities.

The lineage operation list records orientation normalization, opaque RGB
normalization or alpha flattening, fit/downscale, censorship, watermarking, and
encoding. Exact byte determinism is guaranteed for the same inputs, recipe,
renderer version, and Pillow version. The Pillow version is therefore part of
lineage, and upgrades require golden test review.

## Fit and censorship behavior

- `downscale` preserves aspect ratio and never enlarges the master. Its output
  dimensions may be smaller than the configured box.
- `contain` produces the exact configured canvas, centers the fitted image, and
  fills unused space with `background_rgb`.
- `cover` center-crops to the target aspect ratio and produces the exact target
  dimensions. It fails when enlargement would be required unless
  `allow_upscale=True`.
- Mosaic and blur censorship operate only on the normalized relative region of
  the teaser.
- Censorship runs before watermarking, so it cannot blur or pixelate the
  watermark.
- The `full` member/Patreon output is always clean. A configured transparent PNG
  watermark is composited only onto an explicitly requested `x_teaser`.
  Unselected accepted images are rendered with `targets=("full",)` and never
  render or persist an X teaser. A watermark must contain transparent and
  visible alpha and produce an observable pixel change on the X teaser.
- Before scaling or positioning a watermark, the renderer trims only its outer
  fully transparent canvas to the non-zero alpha bounding box. The configured
  width therefore describes the visible watermark bounds, not transparent file
  padding, and the selected corner anchors those visible bounds. Transparent
  holes and partial alpha inside the bounds are preserved.
- The calibrated defaults render the visible bounds at 264,000 millionths
  (26.4%) of the final teaser width with a 12,000-millionth (1.2%) margin based
  on the teaser's shorter edge. Opacity 255 preserves the PNG's authored alpha;
  it does not make partially transparent artwork opaque. The four positions are
  `top_left`, `top_right`, `bottom_left`, and `bottom_right`.
- Newly prepared and replacement X revisions freeze an explicit public profile:
  a metadata-free PNG named `x-teaser.png`, the original admitted source pixel
  dimensions, no upscaling, and no lossy or resizing fallback. The 8192 x 8192
  downscale box is an axis ceiling; the independent 12-million-pixel source and
  output ceilings keep the actual canvas bounded.
- That profile first encodes deterministic PNG compression level 6. If the
  result exceeds X's 5 MiB static-image limit, it retries level 9 as a lossless
  size optimization. If level 9 still does not fit, the job fails terminally as
  `x_lossless_png_too_large`; it never converts to JPEG or reduces dimensions,
  and therefore creates no uploadable derivative output.
- New recipes freeze renderer `pillow-derivative-v6`. The runtime continues to
  execute already-frozen `pillow-derivative-v4` and `pillow-derivative-v5` jobs,
  including frozen JPEG X recipes, with their original encoding and watermark
  geometry. Version 5 retains trimmed visible-alpha watermark placement but not
  version 6's PNG compression retry, so deployment cannot strand or silently
  change an in-flight derivative plan.

## Parser and resource boundaries

The renderer treats image bytes and recipes as untrusted even when a prior stage
has verified them:

- master bytes, watermark bytes, canonical recipe bytes, input dimensions,
  pixels, aspect ratio, target dimensions, output pixels, and encoded output
  bytes all have hard limits;
- a conservative peak-working-set estimate is calculated from header metadata,
  input/output geometry, alpha mode, watermarking, censorship, encoded buffers,
  and a fixed process allowance before `Image.load()` decodes master pixels;
- output encoders write through a bounded buffer and abort at the byte ceiling;
- Pillow decompression-bomb warnings and errors fail closed;
- only single-frame JPEG, PNG, and WebP masters are accepted;
- watermarks must be single-frame PNG images with valid alpha;
- output formats are limited to deterministic JPEG and PNG configurations;
- logical filenames use a strict ASCII basename policy, matching extensions,
  and Windows reserved-name rejection;
- cover rendering crops before resizing, avoiding oversized intermediate
  canvases; and
- Pillow parser failures are redacted into typed derivative errors.

The defaults are deliberately sized for the admitted workload: 12 million
input and output pixels are the absolute geometry ceiling, with a conservative
576 MiB estimated peak ceiling that covers a watermarked maximum-size source.
Defaults also cap masters at 32 MiB, each output at 16 MiB, and input/output
axes at 8192. Raising a geometry or byte ceiling without raising and re-testing
the working-set ceiling is an invalid production change.

Pillow format plugins remain an untrusted native/parser boundary. The production
entry point creates one fresh `spawn` child for one render and never reuses it.
On Linux, that child installs and verifies hard `RLIMIT_AS` and `RLIMIT_RSS`
limits before invoking Pillow decode. The default child limit is 704 MiB: the
576 MiB renderer ceiling plus a 128 MiB interpreter/IPC reserve. Both the parent
and child fail closed if those limits cannot be established. Non-Linux
environments may run direct unit tests, but cannot use the production entry
point.

The parent enforces a hard wall timeout (120 seconds by default), bounds result
IPC from the two configured output-byte ceilings, and decodes a strict JSON
envelope rather than unpickling child-controlled output. Timeout, caller
cancellation, malformed/oversized IPC, and child crashes all terminate the
one-shot child; a stubborn child is killed after a short grace period. Callers
should handle the typed isolation timeout, crash, protocol, and unavailable
errors as failed jobs with retry policy outside this module.

## Color and privacy normalization

EXIF orientation is applied before geometry operations. Opaque RGB masters keep
the transposed RGB buffer directly, avoiding the former RGBA/background/
composite stack. Other opaque modes convert directly to RGB. Only masters with
alpha use an RGBA canvas and the recipe background color. All paths produce the
same metadata-free RGB output contract, and embedded ICC profiles are not
propagated.

Outputs are encoded from a new pixel-only image. EXIF, GPS, prompts, comments,
software paths, ICC payloads, PNG text chunks, and other source-private metadata
are not copied. JPEG files may still contain standard encoder/JFIF structural
fields, which carry no source-private content.

The same container-level privacy check runs when the derivative is registered,
when a finished-set ZIP is built, and immediately before Patreon, X, or MEGA
delivery. A metadata-bearing derivative fails closed at any of those boundaries.
The private raw-master object is never rewritten, so its prompt and generation
settings remain available in the authenticated library.

## Automatic durable execution

`gen_automation.services.derivative_runtime` connects the isolated renderer to
the frozen review and derivative-plan records. One controller cycle performs at
most one sequential job:

1. Claim one durable `derivative_jobs` lease.
2. Confirm that its release version is still current, its release is in
   `rendering`, and its `ReleaseSelection` and approved `DerivativeRecipe`
   snapshots still match the claim.
3. Read the raw master and optional watermark from the private object store at
   their exact frozen version IDs and bounded byte limits.
4. Verify byte length, SHA-256, content type, image format, and signature before
   the isolated process decodes either input.
5. Render the approved targets in a fresh, timeout- and memory-bounded child.
6. Conditionally create each immutable output object, then read that exact
   created version back and verify its metadata, bytes, and digest.
7. Atomically register the `Asset`, `AssetLineage`, and `DerivativeOutput`.
8. Mark the job successful only after every approved target exists. The last
   successful job advances the release to `ready_to_publish`.

The immutable object-key contract is:

```text
derivatives/{release_id}/{release_version_id}/{job_id}/
  {recipe_id}-{recipe_config_sha256}/{source_sha256}/
  {target}/{output_sha256}.{extension}
```

The output SHA is in the key, and creation uses the object store's conditional
write primitive. The controller never overwrites a key. If a write succeeded
but its response or the controller was lost, a retry may adopt the existing
object only after an exact versioned read proves the bytes, digest, content
type, length, and safe metadata all match. Any mismatch fails with
`output_object_conflict` and requires operator investigation.

Database registration and lineage creation share one transaction. A retry can
therefore observe either the complete registration or no registration; it
cannot observe a committed derivative output without its asset and lineage.

## Review handoff and watermark registry

All routes below are under `/api/v1`.

An OWNER onboards a reusable watermark once with multipart
`POST /watermarks` (`release_id`, `display_name`, and PNG `file`) and a stable
`Idempotency-Key`. The service validates the same single-frame alpha-PNG
contract as the renderer in an isolated process, writes the bytes to the
content-addressed `watermarks/{prefix}/{sha256}.png` key, and freezes the exact
object version, SHA-256, dimensions, and byte size in an available registry
asset. `GET /watermarks` lists the registered choices. The provenance release
does not limit reuse: a later recipe may reference the same registered object,
but the database still requires its complete immutable identity to match.

After the owner has chosen up to four X images and the review is completed,
call:

```text
POST /api/v1/review-tasks/{review_task_id}:prepare-derivatives
Idempotency-Key: <stable command key>

{"watermark_asset_id": "<registered asset UUID>", "max_attempts": 3}
```

This one idempotent action creates one clean `full` job target for every frozen
accepted image. Only images frozen in `review_x_selections` also receive an
`x_teaser` target, and selected images require the registered watermark.
Reviews with no X selection omit `watermark_asset_id` and create only clean
full outputs. Replaying the same command does not create another recipe or
another job.

## Operator configuration

Automatic execution is opt-in. Run the controller on Linux, apply the current
database migrations, pass live storage conformance, and set:

```dotenv
GEN_AUTOMATION_BACKGROUND_RUNTIME_ENABLED=true
GEN_AUTOMATION_STORAGE_ENABLED=true
GEN_AUTOMATION_DERIVATIVE_RENDERING_ENABLED=true
GEN_AUTOMATION_BACKGROUND_DERIVATIVE_TIMEOUT_SECONDS=150
GEN_AUTOMATION_BACKGROUND_DERIVATIVE_RENDER_TIMEOUT_SECONDS=120
GEN_AUTOMATION_BACKGROUND_DERIVATIVE_MEMORY_LIMIT_BYTES=738197504
GEN_AUTOMATION_BACKGROUND_DERIVATIVE_LEASE_SECONDS=300
GEN_AUTOMATION_BACKGROUND_DERIVATIVE_RETRY_BASE_SECONDS=30
GEN_AUTOMATION_BACKGROUND_DERIVATIVE_RETRY_MAX_SECONDS=900
```

The job lease must exceed the complete controller-cycle timeout. The cycle
timeout must cover the isolated render timeout plus cleanup. The default
704 MiB process limit is the 576 MiB renderer working-set ceiling plus the
128 MiB process and IPC reserve; lowering it is not a valid production
configuration.

The controller identity needs database access and private object-store
read/write access. It does not need Patreon, X, GPU-provider, or presigned-URL
credentials to render derivatives. No object-store URL or exception text is
stored in job errors or emitted as derivative runtime data.

Enable the flag first in a non-production environment, plan a two-image canary,
and verify both targets and their lineage before enabling it in production.
Keep the flag off on non-Linux hosts: the production isolation boundary fails
closed when it cannot install and verify the hard process limits.

## Finding masters and outputs

Raw masters are never moved or replaced. Their authoritative location and
identity are the `source_storage_backend`, `source_storage_bucket`,
`source_object_key`, `source_object_version_id`, and `source_sha256` columns on
`release_selections`. The ranked review dashboard provides controlled signed
view/download links for the review-stage masters.

Rendered outputs are private `assets` referenced by `derivative_outputs`.
Operators can locate them through the database records or by the immutable
`derivatives/...` prefix in the private bucket using an authorized storage
console or client. `asset_lineage` links each derivative back to the accepted
raw master. There is deliberately no stable public object URL; publishing uses
separately approved derivatives or handoff packages.

## Retry, cancellation, and incident handling

Retryable storage/version availability failures and isolated-renderer timeout,
crash, memory, availability, or protocol failures enter `retry_wait` with
bounded exponential backoff. A controller timeout or shutdown cancellation
also releases the lease into `retry_wait`; an object already written before the
cancellation is reconciled by the exact-match adoption path.

If a process disappears without cleanup, another controller can reclaim the
job after lease expiry. An expired job that has exhausted its frozen
`max_attempts` is dead-lettered as `failed` with
`execution_lease_expired`. Contract, recipe, input, invalid-render, and
immutable-object conflicts fail closed. Job error details use a fixed safe
message; provider errors, local paths, object URLs, and exception messages are
not persisted.

For an incident:

1. Pause new rendering with `GEN_AUTOMATION_DERIVATIVE_RENDERING_ENABLED=false`.
2. Inspect the job's safe error code, attempt count, frozen recipe, selection,
   object version IDs, and controller loop health.
3. For `output_object_conflict`, preserve both the database state and object;
   do not overwrite or automatically delete evidence.
4. Correct the external availability or deployment issue, then resume the
   controller. Terminal recipe/content conflicts require a newly approved
   release version or recipe rather than mutation of the frozen snapshot.

Retries with the same source bytes, watermark bytes, recipe, renderer version,
and Pillow version must produce the same artifact digest. Different digests are
a fail-closed reproducibility incident, never an overwrite opportunity.
