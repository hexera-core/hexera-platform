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
   For FEA, member errors summed bounded the package's true error on every part measured, so the
   ledger is a budget, not a per-feature test. Before the simplified part is promoted, the whole
   package is verified once more as one geometry (T2 on each changed region from one global solve,
   or T3 when the regions overlap); for CFD that package verdict is the only one that counts.
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

### What the bench has measured so far (docs/simplification/DEVLOG.md)

- The ground truth needs boundary conditions on exact faces: a node-band clamp put a 1.07% mesh
  noise floor under compliance, trimmed planar end faces 0.04%.
- T2 against T3 over seven parts and 43 FEA removals: rank 0.99, linear 0.99, sign 23 of 23, median
  ratio 0.94, after one bug was fixed (a clamp face inside the region must keep its clamp). T1 ranks
  holes at 0.98. At a 0.1% compliance budget T2 alone made one false accept, 0.112% read as 0.086%.
- **The savings are in packages**: one harmless feature saves a median 3% of cells; all of a part's
  harmless features together save 14-54%, at a true change of at most 0.08% (0.28% for 54% on one
  part).
- **FEA errors add, conservatively**: the package's true change was within the sum of its members'
  on every part but one, and that one was inside its noise floor. **CFD errors do not**: on a heat
  sink the members summed to about 0.7% of the package's drag change.

So the loop below verifies features one at a time to choose them, then verifies the PACKAGE before
anything is kept. For FEA the package check confirms a ledger that has so far been conservative; for
CFD it is the only verdict that counts.

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
| P3 | Product integration, CRS-shaped | the three rollout slices below: inspect, then attempt behind `CAD_SIMPLIFY_ENABLED`, then the model author; reuse CRS session, caps and packages | each slice proven on `dev-raymond`; corpus runs give cost saved at error spent, vs a rules-only baseline |
| P4 | CFD | adjoint T1, local subdomain T2, OpenFOAM in the mesh image | the same table for a CFD corpus |

## How it fits the repository: the same way CAD repair does

Simplification is integrated exactly as the CAD Repair System (CRS) is. Each row names the CRS piece
and its simplification twin. Where CRS has no such piece, simplification adds none in its first
slice either.

| Concern | CRS today | Simplification |
|---|---|---|
| Package | `cad/repair/`: `contracts.py` (`RepairMode` inspect/repair, `RepairProfile`, `RepairTarget`, `RepairReport`, `RepairResult`), `inspect.py:inspect_geometry`, `conservative.py:repair_step_file` (writes a NEW file, raises `RepairUnavailable` / `RepairRefused`), `session.py:RepairSession` (snapshot, `try_package`, rollback), `caps.py`, `package.py:TargetPackage`, `model_author.py`, `package_review.py` | `cad/simplify/`: `contracts.py` (`SimplifyMode` inspect/simplify, `SimplifyProfile`, `SimplifyReport`, `SimplifyResult`, `FeatureCandidate`), `inspect.py:inspect_features` (recognise + cost attribution, read-only), `simplify.py:simplify_step_file` (new file, `SimplifyUnavailable` / `SimplifyRefused`), `verify.py` (tiers T0-T2), and the CRS `RepairSession`, `TargetPackage` and caps machinery reused, not copied |
| Settings | `settings/cad_repair.py`, kill switch `CAD_REPAIR_ENABLED=false` checked before any kernel call and again in the node; `Group("CAD repair")` in `settings/inventory.py`; `.env.example` and `docs/reference/configuration.md` regenerated from the inventory; `config_inventory.py --full` in `make check-fast` fails on an undeclared env read | `settings/cad_simplify.py`, kill switch `CAD_SIMPLIFY_ENABLED=false`, plus `CAD_SIMPLIFY_MAX_GLOBAL_ERROR` (0.01), `CAD_SIMPLIFY_MAX_LOCAL_ERROR` (0.05), `CAD_SIMPLIFY_TIMEOUT_S`, `CAD_SIMPLIFY_MAX_PACKAGES`, `CAD_SIMPLIFY_LLM_AUTHOR` off/shadow/live (the CRS tri-state); `Group("CAD simplification")`, same regeneration, same gate |
| Graph | `route_after_engine_select` -> `node_repair_inspect` -> `node_repair_attempt` -> `node_geometry_admission`, unconditional edges, no routing function, pinned by `tests/unit/pipeline/test_graph_real.py` | `node_repair_attempt` -> `node_simplify_inspect` -> `node_simplify_attempt` -> `node_geometry_admission`. After repair, because only a valid solid is simplified; before admission, because admission judges whatever geometry is left in place. Same pin test |
| State | `repair_status`, `repair_report`, `repair_attempt`, `repair_lineage`; `STATE_SCHEMA_VERSION` bumped per change (15 now) with a history comment; every field in `pipeline/data_contract.STATE_FIELDS` | `simplify_status`, `simplify_report`, `simplify_attempt`, `simplify_lineage`; version 16; same registry |
| Promotion | `pipeline/repair_promote.promote`: derived id `<source_id>:repaired:<sha16>`, no `geometry_sources` row, unit kept, refused on identical bytes, a unit change, or a file that will not stage; the upload stays the source of record | the same function shape, id `<source_id>:simplified:<sha16>`; a repaired-then-simplified part carries both lineages |
| Where it runs | the Celery `simulation_jobs` worker on the GCE fleet; OCC via `asyncio.to_thread` under `asyncio.wait_for`; one absolute deadline; only `mesh_probe` uses a fresh child interpreter | same worker, same deadline pattern. The verification solves (T2 local pieces, a few thousand C3D10 each) run in a fresh child interpreter as `mesh_probe` does, with CalculiX added to the worker image. Measured on the bench they are seconds; a T3 global solve is minutes and is not run in the product path |
| Events | `"repair"` in `events.STAGES`; `pipeline/repair_words.py` lane line; `pipeline/repair_receipt.py` `RECEIPT` | `"simplify"` stage, `simplify_words.py`, `simplify_receipt.py` |
| Artifacts | `artifact_keys.repair_report_key`, `repaired_cad_key`; `application/repair_report_delivery.py`; `final_result.with_repair_lineage`; `delivery_manifest.built_from_repaired_geometry` | `simplify_report_key`, `simplified_cad_key` (the simplified STEP is a download, optional so `ARTIFACT_POLICY_VERSION` does not move), `simplify_report_delivery.py`, `with_simplify_lineage`, `built_from_simplified_geometry` |
| Console | `render/repair.js` panel, lane line in `render/stage.js`, `run-files.ts` (`repaired_cad`, `repair_report`) | the same three places, `simplified_cad` and `simplify_report` |
| Rules | no family of its own; clocks only (TIME-6, `contracts/work_limits.TIME_REPAIR_CLOCK`, recorded by `engines/determinism.note_time_limit`) | the same: a `TIME_SIMPLIFY_CLOCK` noted the same way, no family in the first slice. Keep/revert verdicts are recorded in the simplify report, as CRS records its op verdicts. Whether they become rulebook decisions is an open question below |
| Deploy | `cad_repair` dispatch input (13 of 25 used); provision env; `create-api-service.sh` `API_ENV_PAIRS`; `create-worker-fleet.sh` `cad-repair-*` metadata mapped to env by `worker/startup.sh`; `tests/unit/deploy/test_cad_repair_opt_out_deploys.py` | `cad_simplify` input (14 of 25), the same three plumbing points with `cad-simplify-*` metadata, and the twin opt-out test |
| Model | `call_repair_review_model` in `contracts/model_inference.py` and `adapters/model_inference/router.py` (rides `INTAKE_ROUTE`), prompts inline in Python, the vendor SDK confined | `call_simplify_author_model`, the same route and confinement, prompts inline |
| Capture | `TrainingLogger` events `repair_inspect`, `repair_attempt`, each declared in `data_contract.EVENT_TYPES`; never load-bearing | `simplify_inspect`, `simplify_attempt`, declared the same way. Every verdict is a training row, which is the learned-prior idea above |
| Tests | `tests/unit/cad/test_repair_*.py`, `tests/unit/pipeline/test_repair_*`, settings and deploy tests, `tests/native/test_native_repair_*.py`, corpus tooling in `devtools/validation/repair_corpus.py` | `test_simplify_*` at the same tiers; `devtools/simplify/` is the corpus tooling twin |

**The rollout copies CRS's order.** CRS shipped diagnostics first ("keep the first feature slice
reviewable: diagnostics only, no geometry mutation"), then conservative mutation behind the kill
switch, then the model-authored packages. Simplification does the same:

1. `simplify_inspect` only: candidates and their attributed cost in the report and the receipt. No
   mutation, no switch needed.
2. `simplify_attempt` behind `CAD_SIMPLIFY_ENABLED`: rules-authored packages, T0 + T2 verification,
   keep or revert, promotion.
3. `CAD_SIMPLIFY_LLM_AUTHOR` shadow, then live.

**Slice 2 as built** (`cad/simplify/simplify.py`, `verify.py`, `pipeline/simplify_attempt.py`,
`simplify_promote.py`; state v17, rulebook v74):

- One driver solve of the fully simplified part, then two local solves per removed feature, cut
  from the original. ccx runs at one thread in a child interpreter.
- A feature is accepted when |estimate| <= 0.5 x budget.
- Accepted features enter a ledger least-error-first while the summed estimate stays within the
  budget; the rest are marked `over_budget`. Only the ledger's features are removed in the
  promoted file.

Departures from the CRS twin:

- `cad_simplify` is a personal-environment opt-in. Shared dev and prod stay off until the check is
  proven there.
- A part that was repaired and then simplified still delivers its repaired CAD, read from
  `simplify_lineage.replaced_geometry`, because `geometry` then names the simplified bytes.

Not yet built (slice 2b):

- A driver of its own for borderline features.
- **The package-level re-check before promotion.** The bench measured a package at 1.16x its
  member sum (KP08, wall-rule size), so the ledger alone is not a bound.
- Calibrating the cut-mismatch limit (0.10 is a placeholder).
- Delivery of the simplified STEP, console panels and lane text.
- User-named fixed/load faces. They are authored after admission today; see the open questions.

`devtools/simplify/` (the harness and `local.py`) stays the measurement bench throughout. It is how
each slice's thresholds are chosen before the slice ships.

## Open questions for the product owner

1. The default error budget, and whether the user states QoIs or we infer them from purpose.
2. Whether the simplified CAD is a deliverable in its own right (a "simplified STEP" download).
3. FEA modal in the first slice or after static.
4. CRS has no rule family, and its verdicts live in its report. Mirroring that is the default. Should
   simplification's keep/revert verdicts instead become rulebook decisions (a `SIMP` family), given
   that they change what the user's simulation answers?
5. CalculiX in the worker image (small local solves in the product path) or a separate solve job
   beside the mesh job.
