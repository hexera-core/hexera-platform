# Mesh-Failure Operations Implementation Plan (Plan 2 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The repair catalogue gains one operation per real-world mesh-failure family — split closed faces, fragment touching bodies, reach open loops, rebuild a face — so located mesh defects can actually be repaired, measured on the 80-file track.

**Architecture:** Each operation follows the existing `RepairOp` protocol in `cad/repair/operations.py` (stateless, declares its evidence, judged by the progress rule), sits behind its own fail-closed flag, and is mapped to its `mesh_*` code in `rules_author.MESH_OPS` so the deterministic baseline exercises it before any model does. 3D-family operations get their per-operation proof from a whole-shape probe, since scoped probes are 2D-only (Plan 1 rulings R8/R11).

**Tech Stack:** Python 3.12, OCP (OpenCASCADE: ShapeUpgrade, BOPAlgo, ShapeFix), gmsh 4.15 via `cad/repair/mesh_probe.py`, pytest.

**Spec:** `docs/superpowers/specs/2026-11-14-mesh-failure-repair-design.md` (approved 2026-11-14; operations table in "B. New operations"). Parent: `docs/superpowers/specs/2026-11-11-scoped-llm-cad-repair-design.md`.

## Global Constraints

- Every operation is behind its own posture flag, default off, fail closed, registered like existing `CAD_REPAIR_*` flags (settings/cad_repair.py, settings/inventory.py, docs/reference/configuration.md): `CAD_REPAIR_SPLIT_CLOSED_FACES`, `CAD_REPAIR_FRAGMENT_BODIES`, `CAD_REPAIR_REBUILD_FACE` (booleans, like `CAD_REPAIR_FILL_PLANAR_HOLES`).
- Flag off is byte-for-byte today: the operation is absent from the catalogue and `MESH_OPS`, and all existing tests pass unchanged (incl. the committed-JSON scan baseline and the 194-passing native repair tier; 3 known `fill_nonplanar` flag-off timing failures and 9 known unit/cad environmental failures are pre-existing).
- Every operation declares its evidence exactly (faces added/removed as counts, `AREA_DELTA`/`VOLUME_DELTA`), and is refused by `sanity_refusal`/`cumulative_refusal` when the declaration is wrong — the `FixSmallFaces` pattern. `CAD_REPAIR_ALLOW_FACE_REMOVAL` is never used.
- An operation answering a 3D-family code (`mesh_edge_through_face`, or any defect whose `measurements["stage"] == "3d"`) cannot be proven by a scoped probe. Its proof is the whole-shape probe. Plan-1 session rules stand: located 3D-family defects are carried through scoped scans and only a whole-shape probe clears them.
- gmsh only via `mesh_probe.probe_file` (fresh interpreter, never fork). Unit tests run without gmsh and OCP where possible; kernel work goes in `tests/native/test_native_repair_*.py`, corpus-backed via the `_corpus()` pattern (env `HEXERA_MESH_FAIL_CORPUS`, default `~/archductor/mesh-fail-corpus`, skip only when absent).
- No corpus CAD is committed. Evidence carries hashes and fetch steps only.
- One commit per task, targeted `git add` of the task's files only. Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- venv is `.venv` (uv-built; `make setup` broken on this host). Gates before each commit: `make lint`, `make check-fast` (regenerate the publication manifest per its own instructions when it complains), `make typecheck` (the `cad/fea_joins.py` `sched_getaffinity` error is pre-existing).

## Review Focus

1. **Two bodies that interpenetrate (shared volume > 0), handed to `fragment_bodies`.** Expected: decline, never a silent re-ownership of the shared volume. Pinned in Task 3 (`test_interpenetrating_bodies_are_declined`).
2. **`split_closed_faces` on a face that is already split or not periodic.** Expected: decline (no-op is a decline, not a kept op). Pinned in Task 2 (`test_non_periodic_face_declines`).
3. **A fragment that raises the solid count.** Expected: declared, so `volume_incomparable` does not silently switch the volume caps off (caps.py:243 lesson from the region-loop branch). Pinned in Task 3 (`test_fragment_declares_its_solids`).
4. **`rebuild_face` moving a feature while every count passes.** Expected: deviation measured and recorded on the op's evidence; the flag stays benchmark-only until Plan 3's reviewer is live. Pinned in Task 5 (`test_rebuild_records_deviation`).
5. **An operation kept for doing nothing because its 3D proof never ran.** Expected: a 3D-family op's verdict comes from a whole-shape probe. Pinned in Task 4 (`test_fragment_is_judged_by_a_whole_shape_probe`).

---

### Task 1: Body-pair census — touch or overlap

**Files:**
- Create: `devtools/validation/body_pairs.py`, `devtools/validation/evidence/2026-11-15-body-pairs/README.md` (+ run JSON)
- Test: `tests/unit/dev/test_body_pairs.py`

**Interfaces:**
- Produces: `classify_pairs(solids: list, *, eps_mm3: float, pair_cap: int, deadline_s: float) -> dict` with keys `overlapping`, `touching`, `pairs_checked`, `complete: bool`; CLI `python devtools/validation/body_pairs.py --manifest devtools/validation/corpora/mesh-failure-real.json --roots DIR... --json OUT` that runs it over every multi-solid PLC-failure file. Pair test: bbox prefilter, then `BRepAlgoAPI_Common` volume > `eps_mm3` means overlapping, else touching. `eps_mm3 = 1e-6`, `pair_cap = 40`, per-file `deadline_s = 120`.

- [ ] **Step 1: Write the failing unit tests** — `test_classify_counts_overlap_and_touch` (fake solids via injected `common_volume` callable), `test_pair_cap_marks_incomplete`, `test_deadline_marks_incomplete`. Run; expect FAIL (module missing).
- [ ] **Step 2: Implement; run unit tests to PASS.** The kernel calls live behind an injectable seam so the unit tests stay hermetic.
- [ ] **Step 3: Run the CLI over the corpus** and write the evidence README: per-file table, headline "N of 23 files have at least one overlapping pair; M are touch-only". A controller probe (2026-11-14, /tmp/fragprobe) found real overlaps on the first file (`bm2_ec_exhaust-A.stp`: 2 overlapping, 1 touching), so expect a mixed answer; report what is measured.
- [ ] **Step 4: Gates, commit** `feat(validation): census of touching vs overlapping bodies on the mesh-failure track`.

### Task 2: `split_closed_faces`

**Files:**
- Modify: `src/meshpipeline/cad/repair/operations.py`, `src/meshpipeline/cad/repair/rules_author.py` (MESH_OPS), settings files
- Test: `tests/unit/cad/test_repair_split_closed.py`, `tests/native/test_native_repair_split_closed.py`

**Interfaces:**
- Produces: `class SplitClosedFaces` with `name = "split_closed_faces"`, `answers = frozenset({DefectCode.mesh_periodic_surface})`, narrow `scope`; `targets_for` returns the defect's face; `apply` uses `ShapeUpgrade_ShapeDivideClosed` scoped by `splice` to the target face's owner, declares `faces_added` (exact count) and zero `AREA_DELTA`/`VOLUME_DELTA`. In `MESH_OPS`: `mesh_periodic_surface -> "split_closed_faces"` (2D stage only, same guard as the overlap mapping).

- [ ] **Step 1: Failing unit tests** — `test_answers_and_targets`, `test_non_periodic_face_declines` (apply on a planar face returns a declined outcome: the fixer reports nothing split), `test_declares_exact_face_count`. RED run.
- [ ] **Step 2: Implement; unit tests PASS.** Decline = the existing declined-outcome convention (see `FillNonplanarHole`'s decline path).
- [ ] **Step 3: Native test** — `test_corpus_periodic_file_is_repaired_and_meshes`: `occt/bug32596_abc_0_13.step` under the region loop with probe live and this flag on; assert the op is kept and the file's whole-shape probe passes afterwards (or, if it still fails, assert the kind changed from `periodic_surface` and report the residual honestly — do not switch fixtures silently). Note: after OCP reads this file it times out at 120 s in the calibration; budget the probe generously (300 s) in this test and report the timing.
- [ ] **Step 4: Flag-off byte-identity** — catalogue absent, MESH_OPS unchanged, native baseline green. Gates, commit.

### Task 3: `fragment_bodies` (touching bodies only)

**Files:**
- Modify: `src/meshpipeline/cad/repair/operations.py`, `rules_author.py`, settings files
- Test: `tests/unit/cad/test_repair_fragment.py`, `tests/native/test_native_repair_fragment.py`

**Interfaces:**
- Consumes: Task 1's `classify_pairs` seam logic (reimplemented minimally inside the op: a bbox-filtered `BRepAlgoAPI_Common` volume test on the two target bodies).
- Produces: `class FragmentBodies` with `name = "fragment_bodies"`, `answers = frozenset({DefectCode.mesh_edge_through_face})`; `targets_for` resolves the defect's face to its owner solid and the nearest intersecting solid; `apply`:
  - common volume > 1e-6 mm³ → **decline** with the reason "the bodies interpenetrate; which body owns the shared volume is a judgement" (Plan 3's model may answer it later);
  - touching → `BOPAlgo_Builder` (general fuse/fragment) over the two bodies, keeping both; declares `solids_delta` (expected 0), `faces_added`, `AREA_DELTA`≈interface area, `VOLUME_DELTA`≈0.

- [ ] **Step 1: Failing unit tests** — `test_interpenetrating_bodies_are_declined`, `test_fragment_declares_its_solids`, `test_answers_only_edge_through_face`. RED run.
- [ ] **Step 2: Implement; unit PASS.**
- [ ] **Step 3: Session proof ruling** — a package whose plan contains a 3D-family op gets its predicate judged with a whole-shape probe: in `session.try_package`'s scoped rescan, when any plan op answers a 3D-family code, pass `within=None` for the verification scan. Test: `test_fragment_is_judged_by_a_whole_shape_probe` (spy scanner records `within`).
- [ ] **Step 4: Native test** on a touch-only corpus file from Task 1's census (pick from its JSON; if the census found none suitable, build a two-touching-boxes BREP fixture — synthetic is acceptable here because the op's input condition, not gmsh failure, is what is under test; note it). Assert kept + declared interface faces. Then one corpus edge-through-face file end-to-end: report whether it now meshes; `abc/00506550...` is the known single-solid case (fragment will decline there — assert the decline reason reaches the report).
- [ ] **Step 5: Flag-off identity, gates, commit.**

### Task 4: `mesh_open_loop` reach — measure, then extend `fix_face_boundary`

**Files:**
- Modify (only if measurement demands): `src/meshpipeline/cad/repair/operations.py` (`FixFaceBoundary`)
- Test: `tests/native/test_native_repair_open_loop.py`; Create: `devtools/validation/evidence/2026-11-15-open-loop-reach/README.md`

- [ ] **Step 1: Measure.** Run the region loop (probe live, existing flags) on the corpus open-loop files (`occt/trj10_ch1-id-chs.stp` reads clean under OCP per calibration — use the file-probe arm as ground truth; `abc/00508775...`). Record per file: was `fix_face_boundary` proposed, kept, did the file mesh after.
- [ ] **Step 2: If `target_remains` dominates,** extend `FixFaceBoundary.apply` with `ShapeFix_Wire` passes (`FixReorder`, `FixConnected`, `FixClosed`) at the target face, declaring nothing new. TDD the extension against the failing corpus case; keep the existing `fix_face_boundary` tests green.
- [ ] **Step 3: Evidence README** with the before/after table, honest about residuals. Gates, commit.

### Task 5: `rebuild_face` (benchmark-only until Plan 3's reviewer is live)

**Files:**
- Modify: `operations.py`, `rules_author.py`, settings files
- Test: `tests/unit/cad/test_repair_rebuild_face.py`, `tests/native/test_native_repair_rebuild_face.py`

**Interfaces:**
- Produces: `class RebuildFace`, `name = "rebuild_face"`, `answers = frozenset({DefectCode.mesh_overlapping_facets})`; `apply` re-trims the target face from its boundary wire (`BRepBuilderAPI_MakeFace` on the surface + outer wire, `ShapeFix_Face` to finish), output parametric B-rep; declares `faces_removed=1, faces_added=1` and measured `AREA_DELTA`; records sampled max deviation of the new face from the old (`evidence["deviation_mm"]`, 32-point sample via `BRepExtrema_DistShapeShape`).
- The docstring and `docs/reference/configuration.md` entry state the gate: **not to be set live on customer jobs until the Plan 3 semantic reviewer is live** (spec: the reviewer is what replaces the numeric deviation gate).

- [ ] **Step 1: Failing unit tests** — `test_rebuild_records_deviation`, `test_declares_one_face_swap`, `test_only_overlapping_facets`. RED.
- [ ] **Step 2: Implement; unit PASS.**
- [ ] **Step 3: Native:** corpus overlapping-facets single-solid file (`abc/00500400...`, 8 faces): run the region loop with the flag on; report kept/reverted, deviation recorded, and whether it meshes after. Honest residuals.
- [ ] **Step 4: Flag-off identity, gates, commit.**

### Task 6: Measure the catalogue on the track

**Files:**
- Create: `devtools/validation/evidence/2026-11-15-mesh-failure-operations/README.md` + sealed bundles

- [ ] **Step 1:** Re-run `repair_corpus.py --mesh-probe --bundle ...` on the 80 files, region loop + probe live, all three new flags on, same base settings as the 2026-11-14 evidence. One arm; compare against the Plan 1 Task 8 live arm as baseline.
- [ ] **Step 2:** Evidence README: `newly_meshed` (was expected ~2 before operations), per-operation kept/declined/rolled-back, `newly_broken` (**must be 0**), per-family residuals, cost. "What this does NOT show" section: no model judgement yet, gmsh ≠ product mesher, reader effect.
- [ ] **Step 3: Commit** `Evidence: mesh-failure operations measured on the real-world track`.

---

**Cross-plan gate carried forward:** `CAD_REPAIR_REBUILD_FACE` stays off on anything customer-facing until Plan 3's reviewer is live. Plans 3 and 4 are separate documents.
