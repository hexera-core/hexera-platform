# Mesh-Failure Repair - Design

Status: draft for owner approval, 2026-11-14. Extends
[the scoped LLM CAD repair design](2026-11-11-scoped-llm-cad-repair-design.md); every decision
recorded there still holds unless this document says otherwise.

## Intent, And Why Now

The goal of repair is a part that meshes. Until now, CRS has been measured on the defects its own
inspector reports, and those turn out to barely predict meshing failure:

- Every NIST MBE file the inspector flags `open_shell` meshes in plain gmsh, including one whose
  five open shells CRS could not repair.
- On 80 public, real-world STEP files that genuinely fail a gmsh volume mesh (33 from the OCCT
  7.8.0 test dataset, 47 of 400 sampled from ABC chunk 0050), CRS as it stands makes **2** mesh.
  For **15** of them the inspector reports nothing at all.

The failures fall into a handful of families. None is in the operation catalogue, and the
inspector's codes don't name them:

| gmsh failure | Likely cause | Known class of fix |
|---|---|---|
| Overlapping facets on one surface | Bad trimming, a degenerate face, or a surface folding on itself | Rebuild or re-trim that one face; fix its wire |
| Segment and facet intersect (PLC), mostly on multi-solid parts | Bodies of an assembly overlap or touch | Fragment the bodies so they share a conformal interface |
| 1D mesh not a closed loop | Gaps or ordering faults in a face's boundary wire | Fix or close the wire |
| Impossible to mesh periodic surface | A closed surface (full cylinder or sphere) with a single seam | Split closed faces (`ShapeUpgrade_ShapeDivideClosed`) |
| Solid present, no volume elements; timeout | Mixed | Diagnose per file |

The owner chose to build all three layers in this document: locate the failures, add the
operations that answer them, and let the model choose among them.

The hypothesis carried over from the parent design: a model that sees a failing area can judge what
it is - a real CAD fault, a deliberate feature the mesher mishandles, or bodies that should share
an interface - and pick the edit, better than a fixed rule can.

## What "Fixed" Means

Agreed with the owner, 2026-11-14. Three checks, each with one job:

| Check | Role |
|---|---|
| **gmsh mesh test** (the probe used to build the corpus) | Fast, in-loop verifier. The model's edits are kept or undone on it |
| **Product mesher** | Final verdict, run once on the winning candidate, on a personal dev slug |
| **CAD barely moved** | Guard. A part that meshes because geometry was removed is a failure, not a fix |

Repair edits only the CAD. When the model judges that the CAD is sound and the mesher is the
problem, it records that verdict and changes nothing; the verdict is a hand-off to the meshing
stage (the parent design's fidelity map). **Mesher settings are never changed by repair.** A
"mesher's problem" verdict counts as correct only when the product mesher later succeeds on the
same, unedited file with adjusted settings - otherwise it is an escape hatch for hard parts.

## Goals

1. Every real-world mesh failure in the corpus is **located**: the failing face (or face pair, or
   body pair) is named by fingerprint and handed to the region loop as a defect.
2. The catalogue gains an operation for each family above, each scoped to a region, snapshotted,
   undone on failure and declared to the caps.
3. The model authors packages over these defects, using the mesh test as its predicate.
4. The 80-file corpus becomes a benchmark track, scored as **files that mesh after repair**, with
   the deviation guard and a no-op control.

## Non-Goals

- Changing mesher settings or choosing engines in response to a "mesher's problem" verdict. The
  verdict is recorded here; acting on it belongs to the mesher work in the parent design (Plan 4).
  The opposite direction - the mesher handing its failures back to repair - IS in scope (see
  Owner Answers).
- Making gmsh the product mesher's definition of meshable. It is the cheap in-loop check; the
  product mesher stays the verdict.
- Committing corpus CAD. ABC models remain their creators' copyright; the track carries hashes and
  fetch instructions, like the NIST track.

## Anti-Goals

These would make the work a failure even if the mesh count went up:

- **Meshing by deletion.** Removing or collapsing the failing faces until gmsh stops complaining.
  The deviation guard and the face-count declaration exist to refuse this.
- **Fusing an assembly the customer meant to keep separate.** Bodies that touch are often separate
  parts on purpose. The default is fragment (keep every body, share the interface); fuse is a
  semantic call the model must state and justify.
- **Detection blocking good files.** Areen's rule (24 Oct, `cad/suspect_defects.py`): our checks
  never block a file; only a mesh that fails at the flagged spot says the CAD needs repair. The
  production route honours it by construction: repair is triggered by the product mesher's own
  failure, never by our probe.
- **"Mesher's problem" as a dodge.** Unverified verdicts score as unresolved.

## Approaches Considered

| | What | Trade-off |
|---|---|---|
| A | Turn mesh failures into located defects; reuse the existing catalogue | Small; finds the 15 invisible files; the catalogue mostly can't fix these families |
| B | A plus one new operation per failure family | Medium; matches the failures actually seen |
| C | B plus the model authoring packages over them | Needs the parent design's Plan 2; gives the model real choices |

**Chosen: all three, in that order** (owner, 2026-11-14). A is the precondition for B, and B is
what gives C anything worth choosing between.

## The Design

### A. The mesh probe as an inspector

A new detector runs the mesh test on the shape in an isolated child process with a work-bounded
timeout, and turns each failure into an `EntityDefect`:

- gmsh names the failing surface by tag. The probe maps tag to OCC face by importing the exact
  shape it is given, so tag order follows the shape's own face map, and from there to the face's
  fingerprint and location as `localize` computes them. **A defect the probe cannot map to a face
  is still reported, unlocated** (the parent design's unlocated region), never dropped.
- New `DefectCode` values, one per family: `mesh_overlapping_facets`, `mesh_edge_through_face`,
  `mesh_open_loop`, `mesh_periodic_surface`, `mesh_no_volume`, `mesh_timeout`. Severity error. None
  is a reserved judgement code.
- **Scoped probing.** The surface families are per-face, so a region is checked by surface-meshing
  only the region's faces - cheap, and it keeps the region loop's cost property. The PLC and
  no-volume families need a volume mesh and are checked on the bodies the region touches.
- The probe runs once at the start of a run, after the existing scan, and as the package predicate
  (below). It is not part of `localize_scan`'s byte-identical baseline.

### B. New operations

Each follows the existing `RepairOp` protocol: declares its evidence (faces added or removed, area
and volume moved), is refused before re-inspection if the declaration is wrong, and is judged by
the progress rule.

| Operation | Answers | Notes |
|---|---|---|
| `split_closed_faces` | `mesh_periodic_surface` | `ShapeUpgrade_ShapeDivideClosed` on the region's faces. Adds faces, moves no area. Deterministic; the first to build |
| `fragment_bodies` | `mesh_edge_through_face` on multi-body parts | Boolean fragment of the overlapping body pair. Keeps every body. Declares interface faces added and any volume shared |
| `fix_face_wire` | `mesh_open_loop` | `ShapeFix_Wire` scoped to one face. Check first whether `fix_face_boundary` already covers it; extend rather than duplicate |
| `rebuild_face` | `mesh_overlapping_facets` | The parent design's scoped local rebuild: re-trim or re-approximate one face from its boundary, parametric B-rep, declared deviation. The riskiest; behind its own flag, built last |

`fuse_bodies` is deliberately absent. Merging bodies is reachable only as a model-stated intent,
added later if the corpus shows the need.

### The predicate gains a meshability claim

`Predicate` gains `meshes: frozenset[fingerprint]`: faces the author asserts will mesh after the
package. `evaluate` checks the claim with the scoped probe, falsifiably, with the same rules as
`cleared`. A claim naming a face nobody reported fails, as a phantom does today. The final whole-part
volume mesh runs once at the end of the run.

### C. The model author

The parent design's Plan 2 author, now with mesh defects in its observation. Per region it sees:

- the defects, including gmsh's own error text;
- each face's surface type, area, bounds and neighbours;
- what earlier attempts on the region did and why they were undone.

It returns one of two things:

- a **package**: intent, plan, predicate, budget - the same shape as today, plus the meshability
  claim;
- a **verdict that the CAD is sound and the mesher is the problem**, with the faces and the reason.
  This edits nothing, is recorded in the report, and is scored as above.

The deterministic rules author stays as the benchmark baseline. It maps each mesh code to its
operation directly, which gives the comparison the model has to beat.

### The benchmark track

`devtools/validation/repair_corpus.py` gains a `mesh-failure-real` track over the 80 files, with
per-input hashes and fetch instructions. The track scores:

- **meshes after**: files passing the gmsh test after repair, out of 80;
- **deviation**: distance from the uploaded part, and solids or faces lost;
- **no-op control**: clean parts, which must stay unchanged and keep meshing.

The NIST and generated corpora keep running alongside, so a gain here can't hide a regression
there.

## Constraints

- Caps, snapshot and undo, scoped re-inspection and the three invariant tiers from the parent
  design apply unchanged to every new operation.
- A package is never a cap waiver (`directives.py`).
- `within=None` stays byte-identical to today's scan.
- Posture: everything new defaults off and fails closed, with its own flag per operation.

## What Is Left To The Builder

- How the probe isolates gmsh: a child process, mirroring `localize`'s `_in_child`, is expected.
- The work limit for the probe, as a count of work rather than a clock where gmsh allows it.
- Whether `fix_face_wire` is a new operation or an extension of `fix_face_boundary`.
- The exact observation format the model receives.

## Risks

| Risk | Mitigation |
|---|---|
| gmsh tag-to-face mapping is wrong, so edits aim at the wrong face | Verify the mapping on known fixtures before any operation consumes it; unmappable failures stay unlocated |
| Some families are mesher problems, so no CAD edit can succeed | That is what the verdict exists for; measured, not assumed |
| `fragment_bodies` changes an assembly's meaning | Default keeps every body; fuse is not offered |
| `rebuild_face` moves a feature | Declared deviation, guard on distance to upload, own flag, built last |
| gmsh passes and the product mesher fails | The product mesher is the verdict; the corpus is re-run there before any claim |

## Calls Made Without The Owner

Settled during the builder check; each is the author's call and open to correction.

1. **The corpus probe is defined and lands in the repo.** gmsh 4.15.2, `occ.importShapes` on the
   STEP file, `MeshSizeMax = bbox diagonal / 40`, `generate(2)` then `generate(3)`, one thread,
   120 s timeout; a file passes when it yields more than zero volume elements. It moves from
   `/tmp/meshprobe` into `devtools/validation/`, and the 80-file manifest (source URL, sha256,
   solids, faces, gmsh error) becomes the track's input list. The in-loop probe imports the
   in-memory shape (via a temporary BREP), not STEP, so the first task **calibrates the two**: on
   the 80 unedited files they must agree, and any disagreement is fixed or recorded before an
   operation relies on the in-loop probe.
2. **Each mesh-targeted operation proves itself with the scoped probe.** The per-operation
   re-inspection for an operation aimed at a `mesh_*` defect includes the scoped mesh check -
   surface families mesh only the region's faces, and `fragment_bodies` volume-meshes only the
   two bodies. Without it, "target remains" cannot fire and an operation could be kept for doing
   nothing. The session's merge of scoped results carries `mesh_*` defects like any other code.
   This costs gmsh time per operation; the progress rule is not relaxed for cost.
3. **The model author is in this plan, and does not touch reserved codes.** The `mesh_*` codes
   are not reserved, so the parent design's question on reserved-code licence does not block this
   work. Escalations are advisory only (no re-delivery). Benchmark reproducibility uses the
   corpus runner's existing recorded-reply cache (`--reply-cache`), so the parent's
   byte-identical replay question is answered for the benchmark by replaying recorded replies.
4. **`fragment_bodies` handles touching bodies only.** Bodies that touch get a shared, conformal
   interface and no new solid. Bodies that interpenetrate are a modelling error with no safe
   default owner for the shared volume, so the operation declines and the region escalates, unless
   the model states which body owns it. Before building, the corpus's PLC files are measured to
   see which case they are. A rising solid count must not switch the volume caps off silently
   (`caps.py:243`); the operation declares the exact solids it adds.
5. **A "mesher's problem" verdict clears nothing.** It is recorded on the region and in the report,
   the region's defects stay undischarged, and in this plan it scores as unresolved. Verifying
   verdicts against the product mesher with adjusted settings waits for the parent design's
   mesher plan.
6. **A failed final volume mesh is reported, not undone.** Committed packages each passed their
   own checks, so they stay; the run stops with a new reason, `unmeshable`, and the report names
   the bodies. The receipt and corpus runner learn the new reason in the same task.
7. **Whole-part failures stay unlocated by design.** `mesh_no_volume`, `mesh_timeout` and any
   failure the probe cannot tie to a face fall into the unlocated region, which is refused by
   design and escalates. They are counted in the track so the gap stays visible.
8. **Product verdict.** The gmsh FEA adapter's default profile, run once at the end of each plan
   phase on a personal slug, not every iteration.

## Owner Answers (2026-11-14)

1. **This ships on customer jobs.** Production traffic is coming soon. Route (author's call on
   which route, open to correction): **the product mesher hands its failures back to repair.**
   The FEA ladder already refuses with the failing faces named when a mesh fails where a check
   flagged the CAD (`engines/fea_driver.py`, rule MESH-2, `CAD_DEFECT_GATE`). That refusal
   becomes a repair request scoped to those faces; repair runs the region loop over them; the
   part is meshed again. Only failing jobs pay. A pass budget bounds the round trips, and the
   job graph gains the edge from mesh failure back to repair. Running the gmsh probe on every
   upload was rejected: it adds seconds to every job to find failures the product mesher will
   name for free.
2. **No numeric "barely moved" gate. Acceptance is a complete LLM semantic review.** Whether an
   edit is acceptable is judged by a model reading the evidence, not by a distance threshold.
   This replaces the deviation guard in "What Fixed Means" above. Design of the review (author's
   calls):
   - **The reviewer is a separate model call from the author**, with its own prompt, and never
     treats the author's intent or self-assessment as evidence. The evidence it reads is
     measured: faces, solids and area moved per package, sampled surface deviation from the
     upload, which faces changed and their neighbours, the author's stated intent, the mesh test
     result, and before/after views where the render path can supply them.
   - **Verdict per committed package: accept, or reject with a reason.** Reject undoes the
     package exactly as a failed predicate does. Unsure is reject.
   - It builds on the existing review agents (`agents/repair_review/`) and their tables rather
     than a second review system.
   - **Deviation is measured and shown to the reviewer, never a gate.**
   - **What stays hard:** the kernel floor (valid shape, manifold output, no solid silently
     lost) and the area/volume declaration accounting. Neither is a "barely moved" limit: the
     first stops the kernel producing garbage, the second catches an operation misdescribing
     what it did, which the reviewer relies on to see the truth.
   - Every review verdict, with its evidence, is recorded in the report for audit and as
     training data.

### What this changes elsewhere in this document

- "CAD barely moved" in the scoring table becomes **the reviewer's verdict**; the benchmark still
  reports measured deviation beside it, so a reviewer that accepts large moves is visible.
- Goal 4's score is files that mesh after repair **and** pass review.
- The anti-goal "meshing by deletion" is now enforced by the reviewer, with face-count
  declarations still hard.
- Production posture follows the existing ladder: `shadow` (author, reviewer and mesh test run;
  nothing is delivered from them) then `live`.

### The risk the owner is accepting, stated once

With no numeric gate, a reviewer that accepts a wrong edit ships a wrong part to a customer. The
literature on this loop is specific: in the progress-mirage study a strong in-band judge accepted
cycles of which 44% were real regressions. The mitigations above (separate reviewer, measured
evidence only, default reject, recorded verdicts, shadow before live) reduce that; the benchmark's
measured deviation beside each accepted package is how it gets watched.
