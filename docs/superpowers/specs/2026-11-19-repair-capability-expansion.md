# Repair Capability Expansion Plan

Status: committed alongside the 2026-11-19 corpus harvest. Figures come from `devtools/validation/evidence/2026-11-19-corpus-harvest/aggregate.json`:
100,000 real parts probed, 9,956 failures in the corpus (9.9%). The ranking rule was written
before the figures so the data judges the plan; where a figure overturned an item, the item says so.

## Starting point

The deterministic catalogue (11 operations) measured a ~8-12% ceiling on the original 80-file
real-world track. Per-family facts carried into this plan:

| Family | Known facts | Open question for the harvest |
|---|---|---|
| Overlapping facets | **5,871 (59%)** — 5,840 volume-stage, **0 surface-stage** | Settled: no surface-stage population exists; `rebuild_face` has no reach on this corpus |
| Edge through face | **2,376 (24%)**, all volume-stage; 982 single-solid (out of fragment's reach), 1,394 multi-solid | Touching-vs-interpenetrating split of the 1,394 still needs the body-pairs census on a sample |
| Open loop | **227**, all surface-stage; `close_wire_gaps` (merged tonight) is the candidate | How many carry genuine gaps vs tolerance-phantom loops: measure with the new op on a sample |
| Timeout / no volume | Timeout 587 + crash 45 (6%); no-volume **168, of which 162 single-solid, median 21.5 faces** — a tiny-shell family that may be sew/fill-reachable | The no-volume family gets a measurement pass (new item 5b) |
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
   rules baseline on kept repairs with zero newly-broken. Corpus: 9,956 files.
2. **Catalogue-precondition filtering in the author's validator.** Refuse a proposal whose
   target fails the op's own preconditions before spending an attempt (the live run burned
   attempts on exactly this). Pure validation, no geometry.
3. **Mesher handback (the production loop).** The FEA ladder already names failing faces on a
   MESH-2 refusal; route that back into repair as a scoped request and re-mesh once. This is the
   designed Plan 4 and the largest structural gain: repair stops guessing what the mesher will
   dislike and answers what it actually disliked.
4. **Wire closure with bounded vertex movement.** The open-loop family's only effective pass
   moves vertices. Built and merged tonight (`close_wire_gaps`, PR #490) with declared movement
   and the reviewer gate. Reach: the 227-file open-loop family, pending the genuine-gap share.
5. **`rebuild_face` reach decision — SETTLED: NO-GO.** The harvest found zero surface-stage
   overlaps in 5,871. The family is mesher-side and model-judgement territory; spend nothing
   further on face surgery for it. The op stays in the catalogue for directive/judgement use.
5b. **The no-volume tiny-shell family (168 files, 162 single-solid, median 21 faces).** New,
   found by the harvest: small open shells the probe cannot volume-mesh at all. Measure whether
   the existing sew/fill catalogue already repairs them (one corpus run, no new code); build
   nothing until measured.
6. **Volume-stage overlap handling on the mesher side — now the single largest lever (5,840 files, 59%).** The honest answer for coincident or
   self-touching boundaries is often tolerant meshing rather than CAD surgery. Evaluate the
   existing fTetWild rung as the fallback for parts whose remaining errors are volume-stage mesh
   codes, selected by the repair report rather than by a new operation.
7. **Interpenetrating-pair ownership as a model judgement.** The declined share of the 1,394
   multi-solid edge-through-face files (plus 982 single-solid files nothing deterministic answers). The author may state which body keeps the shared volume; the package
   reviewer and caps judge it; two rejections escalate. Highest-risk item; last.

## Not in this plan

- Tolerant re-intersection and surrounding-geometry face reconstruction (commercial-suite
  territory; revisit once items 1-7 are measured).
- Defeaturing driven by analysis intent (needs intent context the product does not yet carry).
- Any relaxation of caps, holds, or reviewer gates.

## Measurement discipline

Every item lands with: the benchmark track re-run (now 9,956-file-sized; a stratified sample per
iteration, the full track per release), newly-broken required
zero, the clean control untouched, per-family reach reported against this plan's predictions, and
honest negatives recorded as such.
