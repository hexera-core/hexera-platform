# How often does a real v4 delivery carry an inside-out solid? Once in 367.

LEAD-3 asked, after B26: if inverted solids are common in deliveries, every pre-B26 loss number is suspect.
Measured over **every `delivered.brep` of the v4 wave (`snap-v4-202611240851`) and of the fast run
`fast-202611241829`** - 367 real deliveries across four arms. Probe: `lane-b26/inverted_solids.py`,
raw output beside it.

## It is rare

| arm | deliveries | with an inverted solid |
|---|---|---|
| search_ai | 114 | **1** |
| search_det | 119 | 0 |
| search_seq | 11 | 0 |
| staged | 123 | 0 |
| **all** | **367** | **1 (0.3%)** |

The one is `3e7c2a1197c3` under `search_ai` - 4 solids, one of them -13.46 mm3. That is the part lane C20
took the rule to, and the only delivery in either wave that carries the shape B22 was blind to.

## When it happens it is maximally wrong

On that file, `part_kept` measured both ways:

| | part_kept | verdict |
|---|---|---|
| as B22 measured before B26 | **1.0** | a clean PASS |
| with B26's orientation fix | **0.0004** | refused, `part_volume_lost` |

Not a small error: the complement of the inverted solid covered the whole part, so the rule read the
delivery as keeping **all** of it. (Lane C20 reported 0.697 through the kernel's own path, which caps and
compounds differently; the direction and the conclusion are the same.) So the pre-B26 rule did not merely
mis-measure this file - it would have PASSED it, and it refused it only because four other bodies happened
to fall wholly outside the inverted solid.

## What this means for trust in pre-B26 numbers

**Narrow, not broad.** Exactly one delivery in 367 was measured against an inverted solid, so every other
loss number in v4 - and every gaming-flag row that reads one - stands as measured. The rule was wrong where
it mattered most and nowhere else in these two waves.

Two caveats worth keeping:

- This counts DELIVERIES. `keep_delivered` copies a `delivered.brep` only when the arm shipped something
  other than the input, so these 367 are the states that actually met the delivery gate. Work states that
  were never delivered are not counted, and C15's layer-1 finding is what refuses those.
- 11 of the 367 are `search_seq` deliveries from `fast-202611241829` - the units wrote their delivered BREP
  and then died building the row on the missing `ArmRow.inconclusive_verdicts` field. So the seq arm did
  real repair work in that wave even though it produced no rows, and none of what it delivered was
  inside out.

## The v4 baseline the sequential arm will be compared against

From `fast-202611241829`'s own rows (det and ai only, 17 rows each):

| arm | dev rows | dev clean | clean-split rows | clean | mean wall |
|---|---|---|---|---|---|
| search_ai | 12 | 3 | 5 | 5 | 569 s (dev) |
| search_det | 12 | 2 | 5 | 5 | 477 s (dev) |

Both arms refused exactly one dev file on the catastrophic-loss rule, and no row's loss measurement came
back unmeasured.
