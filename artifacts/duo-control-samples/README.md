# Controlled duo composition samples

These four non-production samples demonstrate the visual target for the
Controlled Duo workflow. They intentionally use two original adult characters
with strongly opposed traits so identity or wardrobe leakage is easy to spot.

The recurring cast is:

- **Character A:** fair skin, short copper-orange bob, green eyes, freckles,
  crescent hairpin, teal aviator jacket and cream knitwear.
- **Character B:** deep brown skin, one long indigo braid, amber eyes,
  geometric gold earrings, and an ivory/navy tailored coat with gold trim.

| File | Composition target | Isolation check |
| --- | --- | --- |
| `controlled-duo-overhead.png` | high-angle shoulder-to-shoulder portrait | hair, eyes, jewelry, skin tone and wardrobe remain character-local |
| `controlled-duo-low-angle.png` | low-angle full-body power pose | distinct silhouettes survive strong perspective and unequal depth |
| `controlled-duo-back-to-back.png` | opposing action diagonals in an environment | identity remains stable through interaction, wind and crossing gesture lines |
| `controlled-duo-diagonal-depth.png` | foreground/background museum two-shot | deliberate scale contrast without an accidental third figure or identity swap |

These images are not evidence that the production Illustrious workflow already
has hard isolation. They are acceptance references for composition, legibility
and trait binding. Production canaries must use fixed seeds and score exact
person count, A-to-B leakage, B-to-A leakage, pose adherence, anatomy and style
consistency before a workflow is promoted.

The prompt strategy used for the samples follows the intended UI contract:

1. shared scene, camera and lighting are stated once;
2. each character receives a complete positive identity description;
3. each character explicitly excludes the other slot's distinctive traits;
4. the composition states exactly two adults and forbids background figures;
5. the action and framing are explicit rather than relying on `left` and
   `right` alone.
