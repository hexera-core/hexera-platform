# Repair Capability Expansion Plan

Status: committed alongside the 2026-11-19 corpus harvest. Figures marked `[HARVEST]` are filled
from `devtools/validation/evidence/2026-11-19-corpus-harvest/aggregate.json` before merge. The
ranking rule is written before the figures so the data judges the plan.

## Starting point

The deterministic catalogue (11 operations) measured a ~8-12% ceiling on the original 80-file
real-world track. Per-family facts carried into this plan:

| Family | Known facts | Open question for the harvest |
|---|---|---|
| Overlapping facets | Largest family; 31/31 were volume-stage on the small track, where `rebuild_face` may not aim | Does a surface-stage population exist at scale? `[HARVEST]` |
| Edge through face | Touching pairs: solved end to end. Interpenetrating pairs: declined by design (ownership is a judgement) | Real share of touching vs interpenetrating `[HARVEST]` |
| Open loop | Faces are kernel-valid; every non-vertex-moving pass measured as a no-op | Family size `[HARVEST]` |
| Timeout / no volume | Unlocatable today (~19% of the small track) | Real share `[HARVEST]` |
| Author (model) | Packages did not survive execution in the live run; verdict form unused; safety machinery held | n/a |

## Ranking rule

Expected meshes-after gain per unit of risk. Risk ordering: prompt/text changes < counted
removals < vertex movement < single-face rebuild < body ownership changes. Items 1-3 add no new
geometry powers.

## The backlog, in build order

1. **Author iteration (no new operations).** Rewrite the author's catalogue text to state each
   operation's preconditions (stage, solids, locatedness); add few-shot examples from real
   kept/rolled-back records; exercise the verdict form on files the product meshes as-is. Score
   each prompt revision on the reply-cache harness against the scaled corpus. Gate: beats the
   rules baseline on kept repairs with zero newly-broken. `[HARVEST: corpus size]`
2. **Catalogue-precondition filtering in the author's validator.** Refuse a proposal whose
   target fails the op's own preconditions before spending an attempt (the live run burned
   attempts on exactly this). Pure validation, no geometry.
3. **Mesher handback (the production loop).** The FEA ladder already names failing faces on a
   MESH-2 refusal; route that back into repair as a scoped request and re-mesh once. This is the
   designed Plan 4 and the largest structural gain: repair stops guessing what the mesher will
   dislike and answers what it actually disliked.
4. **Wire closure with bounded vertex movement.** The open-loop family's only effective pass
   moves vertices. Build it as a gated operation with declared movement and the package reviewer
   required live. Reach: the open-loop family. `[HARVEST: count]`
5. **`rebuild_face` reach decision.** If the harvest finds a surface-stage overlap population,
   extend the end-to-end test set and measure; if volume-stage dominates everywhere, mark the
   family mesher-side and spend nothing further here. `[HARVEST: split]`
6. **Volume-stage overlap handling on the mesher side.** The honest answer for coincident or
   self-touching boundaries is often tolerant meshing rather than CAD surgery. Evaluate the
   existing fTetWild rung as the fallback for parts whose remaining errors are volume-stage mesh
   codes, selected by the repair report rather than by a new operation.
7. **Interpenetrating-pair ownership as a model judgement.** The declined half of the
   edge-through-face family. The author may state which body keeps the shared volume; the package
   reviewer and caps judge it; two rejections escalate. Highest-risk item; last.

## Not in this plan

- Tolerant re-intersection and surrounding-geometry face reconstruction (commercial-suite
  territory; revisit once items 1-7 are measured).
- Defeaturing driven by analysis intent (needs intent context the product does not yet carry).
- Any relaxation of caps, holds, or reviewer gates.

## Measurement discipline

Every item lands with: the benchmark track re-run (now `[HARVEST]`-sized), newly-broken required
zero, the clean control untouched, per-family reach reported against this plan's predictions, and
honest negatives recorded as such.
