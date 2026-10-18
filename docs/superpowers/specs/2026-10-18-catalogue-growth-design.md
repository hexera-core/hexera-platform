# Catalogue growth: the two operations the traces demand

**Date:** 2026-10-18 · **Status:** approved by the user's go-ahead in the running conversation
**Builds on:** slices 6–8 (all shipped on this branch). Same safety machinery, two new instruments.

## Intent

The live traces say precisely where repair capability ends today. On real parts the planner —
rules or model — now reaches every defect it is allowed to, and what still fails falls into two
geometric classes the five-operation catalogue cannot answer:

1. **Non-planar openings.** 14 of 31 corpus fill attempts died `declined:not_planar` — a free
   loop that does not lie in a plane has no operation that can close it.
2. **Self-intersecting surfaces.** The turbine class: both meshers reject the geometry, the
   message names the crossing triangles with coordinates, and nothing in the catalogue addresses
   it (`fix_shape_global` executes and rolls back).

Two new operations, built on the slice-6 contract: each takes targets, declares what it adds
before it runs, executes through the session, and lives or dies by the caps and the re-inspection.

## The operations

### `fill_nonplanar_hole` (scope between fill_planar_hole and fix_shape_global)
- Closes a non-planar free loop with a constrained surface patch (the kernel's plate/filling
  machinery — builder picks between `BRepFill_Filling` and `GeomPlate` and justifies).
- Declares the patch area before the write, exactly as the planar fill does: the patch face is
  built and measured in memory, `area_delta_mm2` goes into `evidence`, and only then is it sewn
  in. Volume is not declared, for the planar fill's reason — whether the shell closed is the
  kernel's answer, and `caps.cumulative_refusal` already records a volume that appears from
  nothing rather than comparing it.
- Bounded by the same `CAD_REPAIR_MAX_HOLE_SPAN_RATIO` as the planar fill, measured the same way
  (`_span(wire)` over the part's diagonal). A loop over the bound is a missing wall, not a hole,
  and is declined `too_large` whether or not it is flat.
- Declines a PLANAR loop (`declined:planar`), so the two fills never overlap: a flat loop has one
  determined patch and `fill_planar_hole` owns it. `answers` is `open_shell`, `scope = 6`, so the
  rules planner reaches it the natural way — the planar fill declines `not_planar`, the pair goes
  ineffective, the non-planar fill is the next claimant.
- Target-taking: one loop, named by the first edge of its boundary, exactly the planar fill's
  `targets_for`. Multi-target is refused, as for the planar fill.
- The patch must not bulge: its bounding box stays within the loop's own box inflated by the
  repair tolerance, and its boundary deviation from the loop edges (the filler's G0 error) is at or
  under `tolerance_mm`. Either failing is a decline (`patch_bulges`, `boundary_error`), not a write.
  G1 to the neighbouring faces is attempted and recorded in `evidence`, never required.
- Gated by its own setting, `CAD_REPAIR_FILL_NONPLANAR_HOLES` (default `false`), riding the same
  deploy/env-pair/metadata/startup path as `CAD_REPAIR_FILL_PLANAR_HOLES` (commit c2217d4), so
  ticking `cad_repair` enables both and an unticked redeploy drops both.

### `heal_self_intersections` (scope between the fills and fix_shape_global)
- Targets FACES whose surface self-intersects or interferes with a neighbour — the class both
  meshers reject with coordinates, the ec_turbine-A sample being the named case. `answers` is
  `self_intersection`, `scope = 7`.
- Detection is the kernel's own analysis, not ours: `BOPAlgo_ArgumentAnalyzer` (self-interference
  mode) or `BRepAlgoAPI_Check` over the shape, its faulty pairs localised to face entities and
  emitted by `localize_scan` as `self_intersection` at error severity, with the partner face
  fingerprints in `measurements["crossing_faces"]`. Today the code is emitted only from wire-level
  `BRepCheck` statuses (`SelfIntersectingWire`, `IntersectingWires`); the face-level finding is new
  and is what the turbine needs. The analysis runs inside `CAD_REPAIR_TIMEOUT_S`; a stopped
  analysis returns the scan as not exhaustive, so the loop stops `scan_incomplete` rather than
  reading a short list as a clean part.
- Repair rebuilds or re-trims ONLY the offending faces — wire self-intersection fixing and
  face re-trimming through `ShapeFix_Wire`/`ShapeFix_Face`/`ShapeFix_IntersectionWire`, scoped to
  the target faces, spliced back through the shared `ShapeBuild_ReShape` like every scoped op.
- **It must never grow into a whole-shape rewrite**; that is `fix_shape_global`'s job. Enforced
  in the op, not hoped for: after the fix, every face not in the target set (and its owned edges
  other than those it shares with a target) is the same `TShape` it was before. Anything else
  touched is a decline (`grew_beyond_targets`), and the input shape is returned unchanged.
- **Declines honestly when the crossing cannot be isolated:** when the analyzer's interfering set,
  closed over `crossing_faces`, exceeds `CAD_REPAIR_MAX_HEAL_FACES` (default 4) faces, spans more
  than one solid (two bodies overlapping is an assembly question, not a heal), or re-analysis of
  the fixed faces still reports the crossing. Declines read `not_isolated`, `multi_body` and
  `crossing_remains`, never a silent no-op.
- Declares its area: the summed area of the rebuilt faces minus the summed area of the originals,
  measured before the splice. A heal whose own declaration exceeds
  `CAD_REPAIR_MAX_AREA_CHANGE_RATIO` of the part's original area declines (`too_much_surface`) —
  re-trimming that moves more surface than the residual allows is not a heal, and an unbounded
  declaration would let it walk past the cumulative cap. Volume is not declared; the 2% residual
  judges it.

## Evidence, counted

From `/tmp/planner-evidence/discriminator-validation.json` (331 OCCT files, loop on,
`FILL_PLANAR_HOLES=True`, `max_deviation=0.001`, `max_tolerance_mm=0.1`):

- **430 repaired defects** credited by re-inspection: 414 `open_shell`, 16 `small_edge`, across
  16 repaired files. This is the baseline every success criterion below is measured against.
- 64 `fill_planar_hole` records, one per file reached: 33 declined before writing (14
  `not_planar`, 19 `too_large`) and 31 written and rolled back by caps or the progress rule. The
  Intent's "14 of 31" sets the 14 `not_planar` declines against the 31 written attempts, which are
  a different bucket; the exact figure is 14 of 64 fill records (14 of 33 declines). The 14 `not_planar` files are this slice's first target list.
- 64 `paired_rim` planner refusals. Bore protection is busy on this corpus, which is why the
  pairing guard below is pinned rather than inherited by accident.
- ec_turbine-A reaches triage as `manual_cleanup` (`open_shell`, `small_edge`, `small_face`) with no
  `self_intersection` located — the face-level finding does not exist yet. That is the gap the
  detection half of `heal_self_intersections` closes.

## Decisions (the user's, from the running conversation)

1. Grow the catalogue by exactly these two operations, chosen from the traces.
2. Same contract: targets, declare-before-write, session execution, caps, rollback, re-inspection.

## My calls (stated, since the user skipped the question round)

1. **Both are first-class catalogue entries, proposable by the rules planner AND the model
   planner through the existing mechanism.** They join `OPERATIONS`; the rules planner reaches
   them by `scope`; the model's catalogue (`agents/repair_review/_wire.py`'s name tuple and its
   one-line descriptions) gains both, and a new unit test pins that tuple to `OPERATIONS`' names so
   the two can never drift again. `llm_plan`'s validator treats `fill_nonplanar_hole` as it treats
   `fill_planar_hole`: exactly one target.
2. **The pairing guard covers every fill, by the operations' own names.** `plan.py`'s
   `op.name == FillPlanarHole.name` and `directives.py`'s fill check become membership in one
   `FILL_OPS` set holding both names; the run-level `refuse_filling` blocks both. A model or rules
   proposal of either fill on a paired rim is refused; only an engineer's individual approval
   plays one, exactly as today.
3. **Non-planar rims need their own bore evidence.** `_paired_rims` reads planar, round rims; a
   bore drilled through a curved wall leaves two SADDLE-shaped rims it cannot see, and the
   non-planar fill would seal it — the same sealed-through-hole failure no cap can see. The guard
   gains a non-planar branch: two free loops whose best-fit axes are collinear within tolerance,
   with comparable mean radii and a drillable gap, pair. A false pair costs one missed repair; a
   missed pair costs a customer's hole, so the branch errs toward pairing.
4. **The judgement hold stands.** `self_intersection` is in `REVIEW_RESERVED_CODES` and stays
   there. With per-defect review on, `heal_self_intersections` plays only through an engineer's
   directive (the slice-7 path); with review off, the rules and model planners propose it freely.
   Whether a crossing is a modelling error or a deliberate overlap is still a person's question
   when a person has been asked to answer it; this slice adds an instrument, not a licence.
5. **The new fill gets its own flag**, because it is the one operation that invents curvature the
   customer never drew. Its patch is constrained, not determined; the planar fill's "exactly one
   surface fits" argument does not carry over, so it does not inherit the planar flag's consent.
6. **Mesher-class proof is mesher-class evidence.** A heal is credited to the turbine class only if
   the repaired shape's tessellation also passes `surface_checks.self_intersection_report` — the
   check the meshers' rejection corresponds to. The kernel analysis is the progress rule; the
   triangle check is the corpus's corroboration, recorded per file.

## Architecture

```
localize_scan(shape)
  ├─ BRepCheck statuses (today)                → wire-level self_intersection, open_shell, …
  └─ self-interference analysis (new, bounded) → face-level self_intersection + crossing_faces
                                                  (not exhaustive if stopped → scan_incomplete)

OPERATIONS (scope order)
  1 fix_small_edges · 2 fix_face_boundary · 4 sew_faces · 5 fill_planar_hole
  6 fill_nonplanar_hole (new) · 7 heal_self_intersections (new) · 99 fix_shape_global

RulesPlanner / DirectedPlanner(llm-planner | engineer | replay)
  └─ FILL_OPS guard (planar + non-planar paired rims) · REVIEW_RESERVED_CODES hold (unchanged)
        → RepairSession.try_op → sanity caps → cumulative caps (declared area) → re-inspect → keep/roll back
```

- Both operations live in `operations.py` beside their siblings, carry the class docstring the
  others carry (what it may touch, what it declares, why each decline exists), and return
  `OpOutcome` with a kernel or sewing mapping and `added` fingerprints for any new face.
- No new cap. The two ops are judged by the caps that exist; what is new is what they DECLARE.

## Goals

1. **Corpus-measured, strictly more:** on the same 331 OCCT files with the same settings plus the
   two new flags on, repaired defects credited by re-inspection is strictly greater than 430.
2. **No new cap violations:** every file kept in the baseline is kept again, and no new kept
   operation trips a cap after the fact (the run's final cumulative check passes on every
   promoted file).
3. **Bore protection untouched:** `paired_rim` refusals are ≥ the baseline's 64 (the non-planar
   branch may add; it may never remove), and no file the baseline refused as paired is filled.
4. Each new operation fixes its targeted defect on a real-kernel fixture or corpus part, with the
   target gone on re-inspection and the verdict basis honestly `geometric` or `kernel`.
5. The corpus tool's tallies (`kept vs rolled back`, `defects actually repaired, by kind`,
   `never exercised`) list both operations — an operation the corpus never reaches is reported as
   never exercised, not omitted.
6. Host gates green; the native tier green apart from the ledgered pre-existing failures.

## Non-goals

- No change to any cap, ratio default, the progress rule, rollback, or the review hold.
- No `fix_shape_global` changes; the heal does not become a second global pass.
- No small-face, non-manifold or invalid-B-rep operation. Those stay a person's call.
- No default-on for either new flag anywhere but the corpus run and a personal environment.
- No committed corpus parts (see Constraints).

## Constraints

- **The OCCT dataset files are NOT committed to this repository** — licence. Validation runs point
  the corpus tool at them on disk (its positional directory argument); only the JSON/log reports
  and the counts derived from them are committed beside the demo evidence.
- Settings via the inventory + docs regen (`settings/cad_repair.py`,
  `docs/reference/configuration.md`, `.env.example` together); config-doc test run directly.
- Everything behind the existing flags: `CAD_REPAIR_ENABLED=false` runs nothing; each new op
  declines with a named reason when its own flag is off.
- Tests first. Known pre-existing failures stand as ledgered; any other failure belongs here.

## Fixtures and validation

- **Non-planar loop: synthetic, committed.** A solid whose one curved face (a cylindrical or
  B-spline cap) is removed leaves a closed non-planar free loop; `tests/cad_fixtures.py` gains it,
  plus a cross-drilled cylinder with its bore wall missing (two saddle rims) for the guard. The
  builder proves both on the real kernel **red-first**: the fill test fails on today's catalogue
  (`declined:not_planar`, nothing kept) before the operation exists, and the guard test fails
  with the operation present and the non-planar branch absent — i.e. it shows the hole sealed —
  before the branch is written.
- **Self-intersection: corpus parts, not committed fixtures.** The kernel's builders do not
  readily produce a self-intersecting face on purpose, and a hand-forged one proves only the
  forgery. The builder tries a synthetic fixture first and keeps it if it is real; otherwise the
  native proof is ec_turbine-A and any corpus part the new detection locates, read from disk by
  path (skipped, with a reason, when the path is absent). Because ec_turbine-A triages to
  `manual_cleanup`, its proof is a directed run (an engineer-source directive aimed at the located
  faces), stated as such.
- Unit, hermetic: the declare-before-write ordering, every decline reason, the `FILL_OPS` guard
  for both names, the `_wire` catalogue pinned to `OPERATIONS`, the grow-beyond-targets check with
  op doubles.

## Deliverables

PR updated with both operations, the detection, the guard extension and the flag plumbing; the
corpus reports (baseline and with-new-ops, same settings) committed beside the demo evidence with
the before/after counts for repaired defects, cap violations and `paired_rim` refusals; host gates
green; a personal-environment run with both flags on, naming the job and the op log read.

## Left to the builder

`BRepFill_Filling` vs `GeomPlate` (justified in the docstring); `BOPAlgo_ArgumentAnalyzer` vs
`BRepAlgoAPI_Check` and the exact ShapeFix classes for the re-trim; the non-planar pairing
thresholds (stated as numbers and pinned by the saddle fixture); decline-reason wording beyond the
names above; the model-catalogue one-liners for the two ops; where the corpus tool prints the
triangle-check corroboration.