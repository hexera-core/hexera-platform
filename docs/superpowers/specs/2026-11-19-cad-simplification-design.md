# Simulation-Verified CAD Simplification - Design

Status: draft, 2026-11-19. Branch `raymond/cad-simplification`, personal environment `dev-raymond`.
This is the "later policy" the CAD Repair System reserved: `settings/cad_repair.py` keeps
`CAD_REPAIR_ALLOW_FACE_REMOVAL` off because "deleting a face is defeaturing, which is a judgement
about what the part is FOR", and `2026-09-22-cad-repair-system-design.md` lists broad defeaturing as
a non-goal of the first release. This document is that judgement, made measurable.

## Intent

Edit a part by removing or replacing features so that it **meshes faster and solves faster**, while
the simulation answers the user cares about stay **within a stated tolerance of the original's**.
The loop is agentic: a model proposes simplifications with a reason, deterministic code performs
and measures them, and nothing is kept that the verification did not accept.

Success is one number pair per part: **cost saved** (cells, mesh seconds, solve seconds) at
**error spent** (relative change of each quantity of interest, QoI). A simplification that saves
nothing is not kept, even if it is harmless; a simplification that saves a lot but spends more error
than the budget allows is not kept either.

## Non-goals (for now)

- No mutation of the uploaded source. The simplified part is a new geometry with provenance, as
  repaired geometry already is.
- No dimensional reduction (solid to shell/beam). `cad/midsurface.py` exists; idealisation is a
  later phase, not this one.
- No CFD in the first slice. FEA (linear static, then modal) is first; CFD is phase 4. Reason:
  the platform already writes CalculiX decks (`engines/fea_writers.py`), a linear solve takes
  seconds to minutes, and the QoIs are unambiguous. A RANS run is hours and convergence is itself a
  source of error that would drown the defeaturing signal while the harness is being calibrated.

## The loop

```
 recognise ──> attribute cost ──> propose ──> edit ──> verify (tiered) ──> keep / revert ──┐
     ^                                                                                       │
     └───────────────────────────── re-plan with the ledger ─────────────────────────────────┘
```

1. **Recognise** candidate features: holes (through/blind, grouped by alike), fillets and rounds
   (concave vs convex is recorded, see below), chamfers, small faces and slivers, bosses, pockets,
   embossed text/logos. Reuse `cad/fea_features.py` (hot spots with type and radius) and
   `cad/part_features.py` (grouped features); add what they lack (chamfers, logos, bosses).
2. **Attribute cost**: for each candidate, how many cells and how much mesh time it is responsible
   for. Two estimates: (a) a priori, from the size field it forces (PLAN-fea-3/-4 already compute
   the element size each face demands, so cells attributable to a feature are
   ∫(1/h_feature³ − 1/h_background³) over its zone of influence); (b) measured, by meshing with and
   without it. (a) ranks, (b) confirms. A feature that costs nothing is never proposed.
3. **Propose**: the model receives the candidates with their cost and their physics context
   (distance to loads and fixtures, local stress from a coarse solve, convexity) and authors a
   *simplification package* per region - the same shape as the scoped-repair target package in
   `2026-11-11-scoped-llm-cad-repair-design.md`: locus, intent in words, plan (catalogue operations),
   predicate stated before execution, budget. Example intent: *"These eight M4 clearance holes carry
   no load and sit in the low-stress flange; fill them."* *"This fillet is at the root of the
   cantilever and is the stress the user asked for; keep it."*
4. **Edit** with OpenCASCADE: `BRepAlgoAPI_Defeaturing` removes a face set and heals the gap from
   its neighbours; filled holes and removed blends go through it. The edit is checked exactly as a
   repair is (`cad/repair/caps.py`, `conservative.py`): valid solid, closed, deviation and volume
   change measured, and the result refused if it left the caps.
5. **Verify**, cheapest tier first, each tier allowed to reject but only the last allowed to
   certify (next section).
6. **Keep or revert** per package, charging the error it spent to the run's **error ledger**.
   Errors from independent features roughly add, so the ledger is a budget, not a per-feature test.
7. **Re-plan**: the ledger and every verdict go back to the model, unlike today's repair pre-pass
   (`cad/repair/plan.py:RunHistory` notes it is not read by the model).

## Verification tiers

The user's proposal, kept as the centrepiece: **a localised simulation of the modified region,
compared with the original.** Around it, a cheaper screen and a final global check.

| Tier | What it computes | Cost | Can it certify? |
|---|---|---|---|
| T0 geometric | volume, area, mass, Hausdorff deviation, centroid and inertia shift | ms | no; rejects edits that moved too much material |
| T1 estimator | from ONE solve of the simplified part: the traction σ·n the simplified solution carries across each removed feature's old boundary, scaled by the feature's size | one global solve shared by all candidates | no; ranks and rejects |
| T2 local submodel | cut a region of radius k·(feature size) around the feature; drive its cut boundary with the global simplified solution's displacements; solve the region with and without the feature; compare local QoIs and the cut-boundary reaction | a small solve per feature, in parallel | yes, for local QoIs, when the cut-boundary reaction matches (Saint-Venant holds) |
| T3 global | full original vs full simplified; every QoI | two full solves | yes; run at the end of a run, and on the corpus to calibrate T1/T2 |

**Why T1 is credible.** Analysis-aware defeaturing (Buffa, Chanon, Vázquez, arXiv 2007.11525;
adaptive version arXiv 2212.05183) proves an a-posteriori estimator of the energy-norm error from
removing a feature that needs only the defeatured solution and boundary integrals over the
feature: for a negative feature (a hole) the original boundary was traction-free, so the error is
driven by how much traction the simplified solution carries across where that boundary used to be.
Feature sensitivity (Turevsky, Gopalakrishnan, Suresh, IJNME 2008, doi 10.1002/nme.2345) gives the
first-order change of a QoI from removing a feature cluster. Both are stated for Poisson/elliptic
problems; linear elasticity is elliptic, so the structure carries over, but **the constants are
ours to calibrate** against T3 on the corpus. Until calibrated, T1 only ranks.

**Why T2 needs its own check.** A submodel is only valid if the cut is far enough that removing the
feature did not change the field at the cut. T2 therefore measures that directly: the reaction
force on the cut boundary with the feature vs without. If they differ by more than a threshold the
region is grown and re-solved; if it cannot be grown inside budget, T2 abstains and the feature
goes to T3.

## Physics rules worth writing down now

- **A concave fillet in a loaded region is never removed for a stress QoI.** Removing it creates a
  re-entrant corner whose linear-elastic stress is singular: the "simplified" peak stress then
  grows with mesh refinement instead of converging. Convex rounds (outside edges) are safe by the
  same argument. Concave fillets are removable only where the local stress is low (T1) or the QoI
  is global (stiffness, frequency).
- **Features under a load or a fixture are not candidates.** Their faces carry the boundary
  condition; removing them changes the problem, not the discretisation.
- **Holes are not small just because their radius is.** A hole's effect on stress is ~3x local
  (Kirsch) regardless of its size; only its distance to the stress the user cares about makes it
  removable. Size governs cost, location governs error.
- **Removing material shifts mass and stiffness.** For modal QoIs the mass change matters as much
  as stiffness; T0 records both.

## Quantities of interest

FEA linear static: compliance (energy), max displacement, reaction forces at fixtures, max von
Mises over the user's regions of interest (and over the part excluding a radius around each removed
feature, so a removed stress riser is not counted as an error at its own location), stress at named
probe points. FEA modal: first N natural frequencies. Default budget: 1% on global QoIs, 5% on
local ones. These are proposals, to be fixed with the product owner.

## Ideas beyond the first slice

- **Adjoint screening for CFD.** OpenFOAM's adjoint solvers produce a surface sensitivity map: how
  much drag, pressure drop or heat flux changes per unit normal displacement of each boundary
  face. A feature's removal is a known normal displacement field, so its first-order effect on the
  QoI is one surface integral. That is T1 for CFD, and it is cheaper than the structural one.
- **Wetted vs dry.** For internal CFD, features not on the fluid boundary are free to remove for
  the flow QoI; for conjugate heat transfer they matter only through conduction.
- **Cost attribution from the mesher's own output.** Mesh the original once, map each cell back to
  the nearest CAD face, and the features that dominate the cell count are visible directly.
- **A learned prior.** Every verdict (feature descriptor, context, error spent, cost saved) is a
  training row; over time a predictor proposes better first guesses and T2/T3 run less often.
  `capture/` already collects operations; the `defect-cad-rag` work is a natural neighbour.
- **Replace, not only remove.** A pattern of small holes can become one equivalent-stiffness
  region; a fillet can become a chamfer of the same stiffness at a fraction of the cells.

## Phases

| | Phase | Deliverable | Evidence it worked |
|---|---|---|---|
| P0 | Infrastructure | `dev-raymond` deployed; a bench VM with gmsh, OCP and CalculiX; corpus (NIST public-domain parts plus synthetic parts with known features) | a corpus part meshed and solved on the bench |
| P1 | Ground-truth harness, no AI | recognise, remove one feature at a time, mesh and solve both (T3), record cost saved and error spent per feature | a table per part; the harness is what every later phase is scored against |
| P2 | Cheap verifiers | T1 estimator and T2 submodel, calibrated against P1 | T1/T2 rank features as T3 does; false-accept rate measured |
| P3 | Agentic loop | packages, ledger, re-planning, behind a flag; reuse CRS session, caps and directives | corpus runs: cost saved at error spent, vs a rules-only baseline |
| P4 | CFD | adjoint T1, local subdomain T2, OpenFOAM in the mesh image | the same table for a CFD corpus |

## How it fits the repository

- **Rules.** Every decision the loop makes needs a rule ID in `docs/RULES.md` with a version bump
  (`tests/unit/hygiene/test_rulebook.py`). A new family, `SIMP`, reserved as `planned` until the
  code that cites each rule lands.
- **Switches.** `CAD_SIMPLIFY_ENABLED`, off by default, independent of `CAD_REPAIR_ENABLED`.
- **Where it runs.** Recognition and editing are OCC work and run in a child process (OCP holds the
  GIL; the semiconductor devlog records uvicorn killing workers that ran it in-process). Meshing and
  solving run off-box: the mesh job for meshes, the bench VM for solves until a solve job exists.
- **The original is immutable** and retrievable; the simplified part carries provenance (which
  packages, which verdicts, error spent).

## Open questions for the product owner

1. The default error budget, and whether the user states QoIs or we infer them from purpose.
2. Whether the simplified CAD is a deliverable in its own right (a "simplified STEP" download).
3. FEA modal in the first slice or after static.
