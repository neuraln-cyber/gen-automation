# Controlled Duo generation

Controlled Duo is the two-character generation profile. It replaces the old
assumption that two overlapping left/right prompt rectangles are sufficient to
keep identities separate.

The legacy regional workflows remain immutable and executable for historical
releases. They are labelled **soft isolation** and must not be presented as a
guarantee that character traits remain local.

## Why the legacy result failed

The original couple graph applies Character A to the left 55 percent of the
canvas and Character B to the right 55 percent. The centre 10 percent receives
both identities. Both regions span the full image height, all LoRAs patch one
shared model, pose is left to text alone, and one generic face-detail prompt is
applied to every detected face.

That combination produces the observed failure modes:

- hair, eyes, clothing and accessories cross from one character to the other;
- faces converge toward one template during generic face refinement;
- the model invents or duplicates a third person when it cannot resolve the
  requested two-person geometry;
- the composition collapses into a static side-by-side arrangement;
- the pair workflow drifts stylistically from the single-character workflow.

This is a known multi-subject diffusion problem, not a vocabulary problem.
[FastComposer](https://arxiv.org/abs/2305.10431) describes identity blending
from unrestricted subject attention and uses attention localization. [Isolated
Diffusion](https://arxiv.org/abs/2403.16954) isolates and resynthesizes subjects
to reduce concept bleeding.

## Production profiles

The first release uses only exact-pinned ComfyUI core nodes. It does not install
extensions at runtime and it does not add an unreviewed model artifact.

### Balanced

Balanced mode performs one base sampling pass. Character A and B receive
separate positive and negative conditioning through two disjoint, feathered
masks. Scene, camera, interaction and shared style conditioning cover the full
canvas.

Standard uses the requested base step count. Draft binds a real reduced base
budget of `min(requested, max(8, ceil(requested * 0.60)))`; it is not a UI-only
label.

This mode costs approximately the same as the legacy pass and materially
improves attribute binding, but the full latent is still diffused together. It
must be described as **stronger guidance**, not perfect isolation. This is also
the limitation documented by attention-coupling implementations: masked
attention still operates on a shared latent.

### Strict

Strict mode runs:

1. the same masked shared + A + B composition pass as Balanced, so both
   identities and poses exist before repair;
2. a masked Character A refinement pass with A-only local identity traits plus
   the shared scene/camera constraints, and no Character B slot conditioning;
3. a masked Character B refinement pass with the corresponding B-only local
   identity traits, and no Character A slot conditioning.

The two masks are disjoint. Each regional sampler receives a latent noise mask,
so diffusion updates remain bounded to its selected region. This is the closest practical
option to no prompt bleed without moving to a different model family. Contact
seams, crossed limbs and heavy occlusion can still require another canary or a
manually adjusted layout.

Strict has three sampler invocations, but the regional invocations use bounded
step budgets. At normal step counts, Standard is approximately `1.0 + 0.5 +
0.5 = 2.0x` the Balanced Standard UNet-step budget. Draft is approximately
`0.6 + 0.25 + 0.25 = 1.1x` that baseline. Noise masks do not make an individual
UNet step cheaper; the savings come from fewer actual steps. Hires refinement
belongs only on accepted candidates.

## Prompt ownership

The UI and frozen release contract separate prompts by responsibility.

| Field | May contain | Must not contain |
| --- | --- | --- |
| Scene and style | setting, medium, palette, lighting, quality | character names, identity triggers, slot-specific hair/clothing |
| Camera | lens, angle, crop, depth hierarchy | identity attributes |
| Interaction | shared relationship/action, gaze, contact point | appearance attributes |
| Character A | A identity, appearance, outfit and action | B identity or attributes |
| Character A negative | traits that must not appear in A | shared quality negatives |
| Character B | B identity, appearance, outfit and action | A identity or attributes |
| Character B negative | traits that must not appear in B | shared quality negatives |

Exactly-two population instructions are system-owned. The preflight warns on
`solo`, `1girl`, conflicting person counts, subject triggers in the shared
scene, or references to the other slot's exclusive traits.

All currently approved LoRAs remain **shared style LoRAs**. A mask does not
spatially isolate a conventional `LoraLoader` model patch. Character-specific
LoRA lanes remain unavailable until a separately audited spatial LoRA-routing
engine is approved. [FreeFuse](https://github.com/yaoliliu/FreeFuse) is a useful
future benchmark, not a production dependency in this release.

## Composition presets

Presets freeze camera intent and initial disjoint mask geometry. The worker
converts the same preview percentages into 8-pixel-aligned, bounded rectangles;
`x`, `y`, `width`, and `height` therefore vary with the preset instead of every
preset silently using full-height lanes. They are not merely prompt snippets.

- **Close portrait:** shoulder-to-shoulder framing, distinct head zones, one
  intentional contact point.
- **Overhead:** high camera, diagonal head placement and outer limbs framing
  the image.
- **Low angle:** full-body power composition, strong foreground foreshortening
  and two clean silhouettes.
- **Diagonal depth:** one character foreground-left and the other
  background-right, with unequal scale but no mask overlap.
- **Back to back:** touching shoulders as the central anchor and opposing action
  diagonals.
- **Full body:** readable head-to-foot silhouettes with balanced negative space.

The principles are drawn from common editorial and character-pair composition:
asymmetrical hierarchy, opposing body angles, a clear contact point, deliberate
depth, color separation and gesture lines that direct attention. The presets do
not reproduce an individual artist's style.

Pose ControlNet is the next structural layer. Official ComfyUI documentation
describes pose keypoints as a ControlNet condition for more predictable
geometry: [ControlNet workflow guide](https://docs.comfy.org/tutorials/controlnet/controlnet).
It requires a reviewed ControlNet artifact, pose asset provenance and signed
worker delivery, so it is deliberately not smuggled into this core-only change.

## Optional identity references

Masked IP-Adapter inputs can provide separate visual references in a future
profile. The upstream [IP-Adapter](https://github.com/tencent-ailab/IP-Adapter)
is designed to combine image prompts with text and other controls. Production
adoption still requires a private, content-addressed reference registry,
CLIP-Vision model onboarding, rights evidence, exact worker download grants and
an audited ComfyUI integration.

Reference upload controls must remain disabled until all of those bindings
exist. A decorative upload field that does not affect the frozen graph is not
acceptable.

## Acceptance gate

Every candidate Controlled Duo workflow is evaluated with fixed seeds and
adversarially different characters across all six presets. Each result records:

- exactly-two-person pass/fail;
- A-to-B trait leakage;
- B-to-A trait leakage;
- identity swap;
- incorrect pose or framing;
- face/detail refinement drift;
- anatomy failures;
- style consistency with the single-character baseline.

The sample images in `artifacts/duo-control-samples/` are visual targets, not
proof that the production checkpoint has passed this gate. A production canary
must freeze checkpoint, workflow, LoRA, prompt, seed and mask identities and be
approved before the workflow is used for a large release.

## Cost policy

- Use Balanced Draft for six one-image composition canaries.
- Promote one or two compositions to Balanced Standard for seed comparison.
- Use Strict only for the approved composition/seed family.
- Apply hires or face refinement only after visual acceptance.
- Keep dashboard previews and rejected candidates short-lived under the normal
  lifecycle policy.

High quality is deliberately unavailable in these two v2 workflow approvals.
The worker fails closed if a workflow claims `duo_high_quality` without a
separately reviewed high-quality topology.

This preserves the existing cost work: the expensive isolation passes are paid
only for candidates that have already demonstrated useful composition.
