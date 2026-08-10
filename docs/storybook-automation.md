# Automated content-rated storybooks

The Storybook feature is a one-scene-per-page pipeline. It does not create comic
pages containing panel grids. Every page owns exactly one selected or generated
full-scene image plus a deterministic lettering overlay.

The initial contract lives in `domain/storybooks.py`; the provider-neutral
placement and SVG preview foundation lives in `services/storybook_layout.py`.
`services/storybooks.py` resolves selected sources and cast identities from
server-owned approvals. No database migration, planner endpoint, font package,
or live route is enabled by this first slice.

## Input modes

`selected_images` preserves ordered `ReleaseSelection` identities from one exact
release version. One selected image is one page. The planner may write the
story, dialogue, narration and SFX, but it cannot replace or reorder an image
without a new immutable plan. Each page freezes the selected asset plus its
source SHA-256 and exact object VersionId; callers cannot submit arbitrary Asset
IDs. The source release must belong to the exact requested project.

`idea_only` freezes a page count and a general idea. The planner supplies one
scene prompt per page, including character blocking, individual poses,
interaction and camera intent. The control plane will later compile those scenes
through the existing approved New Automation generation path. It must never let
the planner choose arbitrary workflows, model files, URLs or storage keys.

Both modes require an explicit content profile. `sfw` remains the safe default;
`nsfw_adults_only` permits explicit fictional-adult sexual dialogue, narration,
SFX, scene prompts and imagery after an explicit attestation. This is not an
SFW-only contract. The client supplies only requested approval IDs; the control
plane loads and locks the current `SubjectApproval` rows and freezes approval
version, canonical-source hash, canonical age and evidence hash. It requires
fictional, clearly adult, non-aged-up subjects with distribution and adult
derivative rights. A caller- or planner-supplied age/hash is never trusted.
Extra image candidates belong behind an explicit cost control because they
multiply GPU work.

## Cost-aware pipeline

The intended order is:

1. Create one bounded story plan for the complete story, not one model call per
   page.
2. Let the operator inspect and edit beats, scene prompts and dialogue before
   generation.
3. In selected-image mode, use the chosen assets directly and schedule no GPU
   work.
4. In idea-only mode, generate one image per page in ordered batches while one
   warm Salad worker is available. Regenerate only pages the operator rejects.
5. Independently assess every source image plus the idea, summaries, prompts,
   poses, dialogue, thoughts, narration and SFX against the pinned SFW or
   adults-only profile.
6. Place and render lettering locally on CPU. Text, bubble and SFX edits rerender
   only the changed page and never start a GPU.

The UI cost quote should keep model planning, safety assessment, scene generation
and CPU rendering separate. An unknown planner or GPU outcome charges its
reserved ceiling. The system must not silently generate multiple alternatives.

## Lettering styles

The visual references supplied by the operator inform general traits only. No
sample wording, artist lettering or exact bubble silhouette is retained.

- `classic_light`: warm-white linked/rounded bubble, dark outline and text.
- `classic_inverse`: dark linked/rounded bubble, pale outline and text.
- `soft_cloud`: irregular-looking dark backing with accent handwriting.
- `accent_float`: bubble-free colored italic dialogue with a dark halo.
- `thought_whisper`: soft cloud treatment for thoughts and quiet copy.
- `impact_sfx`: large outlined display lettering with bounded rotation.
- `narration`: compact high-contrast caption capsule.

Motion streaks are a separate effect layer and should not be baked into a
lettering style. The image model must never be asked to draw final words: vector
or CPU lettering provides reliable spelling, editable copy and zero-GPU reruns.

The current SVG renderer is explicitly a preview renderer and uses browser font
fallbacks. Production output requires a small bundled font manifest with exact
font files, licenses and SHA-256 identities. The browser and Pillow renderer must
consume the same style and font manifest. Runtime font downloads are forbidden.

## Placement and readability

All positions use normalized integer millionths, so a plan is independent of
preview and export resolution. The deterministic first-pass solver evaluates
eight safe-area candidates in reading order, strongly penalizes protected
face/focal regions and existing lettering, and preserves explicit placement
hints when possible.

Crowded pages fail visibly with `manual_review_required`; they do not silently
cover a face, clip copy or shrink lettering below the readability floor. The
editor will expose drag, resize, tail targeting, auto-place and reset controls.
Long copy should be split at sentence boundaries into linked bubbles rather than
rendered as tiny text.

## Frozen content review and provenance

The planner response is strict JSON and includes:

- the exact request hash;
- pinned planner model and immutable revision;
- fixed prompt and schema hashes;
- an ordered, contiguous page list;
- known character keys only;
- one server-injected selected source or one generation intent per page;
- bounded dialogue, narration and SFX elements using allow-listed styles;
- no object, approval, source or content-assessment identities.

The draft is bound to the full internal planner-request hash and the compiler
checks the exact fixed prompt and output-schema hashes. The planner cannot mark
its own work approved. Before final rendering, the selected/generated image and
its complete planned copy require a separate pinned assessment bound to the
request hash, plan hash, ordered source hashes, adult-subject gate and rating.
SFW material must not be silently upgraded to adult. Under the adults-only
profile, consensual explicit fictional-adult material is allowed rather than
rejected merely for being sexual; minors, aged-up or ambiguous-age characters,
unapproved people and real-person sexual content remain rejected. Review,
unavailable and malformed outcomes block generation/render/export. The existing
anatomy assessment cannot provide this guarantee because its fixed prompt
intentionally ignores sexual content.

Assessment JSON is not a client credential. Production finalization must load a
persisted assessment row written by the private semantic-gateway workflow, then
rebuild the source context from the database and revalidate every current
subject approval and selected source before accepting it. This foundation adds
the immutable binding and database revalidation service, but not the persistence
table or public finalization route; rendering/export therefore remains disabled
until that server-owned record boundary is implemented.

Selected-image planning will send deterministic, bounded private image proxies
alongside their frozen source hashes. Hashes alone are not presented as if the
planner could understand the scene. Proxy construction and the multimodal
gateway transport remain part of the next vertical slice.

Final page identity will include the source image SHA-256, overlay SHA-256, font
manifest SHA-256, renderer version and Pillow version. Editing creates a new
immutable story version or page render; completed specifications are never
rewritten.

## Next vertical slice

Ship selected-image stories first:

1. add owner-scoped story, immutable version, page and background-job tables;
2. add fixed-schema planning and content-rating routes to the private semantic
   gateway;
3. choose 2-8 accepted images from one release and preserve their order;
4. provide a storyboard editor with one full image per page;
5. bundle reviewed open-license fonts and add the isolated Pillow compositor;
6. render and download deterministic, correctly rated page assets.

Idea-only compilation can then reuse New Automation, Controlled Duo/Trio,
wildcards and the Experiment warm-GPU lifecycle behind a separate feature gate.
No ComfyUI extension is required for the Storybook foundation.
