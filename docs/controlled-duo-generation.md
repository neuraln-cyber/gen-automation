# Controlled Duo generation

Controlled Duo is the two-character generation profile. It replaces the old
assumption that two overlapping left/right prompt rectangles are sufficient to
keep identities separate.

Controlled Trio v1 extends the same ownership model to three characters. Duo
and Trio both separate identity and appearance, individual pose, shared
interaction, and camera direction so the operator can describe each scene
freely. The supplied layout examples are optional identity-region guides, not a
closed list of poses.

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

The first releases use only exact-pinned ComfyUI core nodes. No custom extension
is installed for Controlled Duo or Controlled Trio, neither profile installs
extensions at runtime, and neither adds an unreviewed model artifact.

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
| Combined pose / interaction | shared relationship/action, gaze, contact points and coordinated geometry | appearance attributes |
| Character A identity | A identity, appearance and outfit | B or C identity and attributes |
| Character A pose | A body direction, gesture, expression and action | B or C identity and attributes |
| Character A negative | traits that must not appear in A | shared quality negatives |
| Character B identity | B identity, appearance and outfit | A or C identity and attributes |
| Character B pose | B body direction, gesture, expression and action | A or C identity and attributes |
| Character B negative | traits that must not appear in B | shared quality negatives |
| Character C identity | C identity, appearance and outfit | A or B identity and attributes |
| Character C pose | C body direction, gesture, expression and action | A or B identity and attributes |
| Character C negative | traits that must not appear in C | shared quality negatives |

Character C fields exist only in Controlled Trio. Exactly-two and exactly-three
population instructions are system-owned by their respective contracts. The
preflight warns on `solo`, `1girl`, conflicting person counts, subject triggers
in the shared scene, or references to another slot's exclusive traits.

### Freeform prompts, wildcards, and batch overrides

The set-level identity and appearance fields are persistent defaults for the
approved cast. Each character has a separate freeform pose field, while the
combined pose / interaction field describes coordinated action or contact
across the complete pair or trio. Camera and framing remain a separate
freeform field. None of these fields is restricted to the example poses used
during design.

Current `__wildcard__` tokens may be inserted into the A, B, or C identity,
pose, and negative fields, the combined interaction, and the camera field. A
multi-character pose wildcard should make every line one complete coordinated
pose for the whole cast. The selected wildcard versions are frozen with the
release and each resolved value is recorded with generation evidence.

Every batch may override the character identity, pose, and negative fields plus
the shared interaction and camera. An override left off inherits the set-level
value. An enabled non-empty override replaces it for that batch; an enabled
blank override explicitly clears it. This allows back-to-back batches to change
individual or group posing without changing the approved cast or rebuilding
the set-wide profile.

All currently approved LoRAs remain **shared style LoRAs**. A mask does not
spatially isolate a conventional `LoraLoader` model patch. Character-specific
LoRA lanes remain unavailable until a separately audited spatial LoRA-routing
engine is approved. [FreeFuse](https://github.com/yaoliliu/FreeFuse) is a useful
future benchmark, not a production dependency in this release.

## Composition presets

Presets freeze initial disjoint identity-region geometry and may suggest
editable camera wording. The worker converts the same preview percentages into
8-pixel-aligned, bounded rectangles; `x`, `y`, `width`, and `height` therefore
vary with the preset instead of every preset silently using full-height lanes.
They guide where each identity should remain legible. They do not select a
pose, restrict freeform pose or interaction text, or guarantee deterministic
limb and contact geometry.

- **Auto / flexible:** neutral disjoint identity regions with the most freedom
  for operator-authored pose and interaction text.
- **Close portrait:** compact regions that keep both faces legible.
- **Overhead:** diagonal upper-image identity regions suited to a high camera.
- **Low angle:** vertically extended regions suited to full-body depth.
- **Diagonal depth:** unequal foreground and background identity regions.
- **Back to back:** opposed regions around a central boundary.
- **Full body:** full-height regions with balanced negative space.

The principles are drawn from common editorial and character-pair composition:
asymmetrical hierarchy, opposing body angles, a clear contact point, deliberate
depth, color separation and gesture lines that direct attention. The presets do
not reproduce an individual artist's style. The pose and interaction prompts,
not the preset name, remain the creative instruction.

## Controlled Trio v1

Controlled Trio v1 requires exactly three distinct, currently approved,
clearly-adult fictional subjects. Its approval must explicitly declare the
`controlled_trio_v1` capability; a normal, legacy-couple, or Controlled Duo
workflow cannot be silently substituted.

The bundled Balanced graph gives A, B, and C separate masked positive and
negative lanes, combines them with the full-frame scene, interaction, camera,
and shared style, and runs one base sampler. Its **Auto / flexible**,
**three-column**, **triangle**, and **layered-depth** choices are disjoint
identity-region layouts only. All three individual poses and the complete
three-character interaction remain freeform.

This first trio contract is Balanced and core-node-only. Draft and Standard
change the real base step budget. Strict regional repair and High quality are
not available until separately reviewed topologies declare those capabilities.

An opt-in pose-map ControlNet profile is a possible next structural layer.
Official ComfyUI documentation describes pose keypoints as a ControlNet
condition for more predictable geometry:
[ControlNet workflow guide](https://docs.comfy.org/tutorials/controlnet/controlnet).
It would require a reviewed ControlNet artifact, pose-map provenance, explicit
UI and signed-worker inputs, and a separate canary worker. The current estimate
is roughly 2.5 GB of additional cold-start artifacts, so this must remain a
separate costed capability rather than being added to every worker. It is not
installed or promised by this release.

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
The shared approval contract rejects `duo_high_quality`, and the worker still
fails closed, until a separately reviewed high-quality topology is implemented.

This preserves the existing cost work: the expensive isolation passes are paid
only for candidates that have already demonstrated useful composition.
