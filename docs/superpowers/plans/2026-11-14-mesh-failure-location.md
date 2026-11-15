# Mesh-Failure Location Implementation Plan (Plan 1 of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CRS can see why a part fails to mesh: a gmsh probe turns each meshing failure into a located defect on a named face, the region loop carries those defects, and the corpus runner scores "meshes after repair" on the 80-file real-world track.

**Architecture:** A new `cad/repair/mesh_probe.py` runs gmsh in a forked child (reusing `localize._bounded`), classifies its error, and maps gmsh surface tags to OCC faces by bounding box. Behind a fail-closed posture flag, the repair session appends those defects to every scan, whole-shape and scoped, so the existing package predicate (`cleared`) can claim a face will mesh. No new repair operation lands here; Plan 2 adds them.

**Tech Stack:** Python 3.12, OCP (OpenCASCADE), gmsh 4.15 Python API, pytest.

**Spec:** `docs/superpowers/specs/2026-11-14-mesh-failure-repair-design.md` (approved 2026-11-14). Parent: `docs/superpowers/specs/2026-11-11-scoped-llm-cad-repair-design.md`.

**The four plans:** 1 (this) locate mesh failures and score them. 2 new operations: `split_closed_faces`, `fragment_bodies`, `fix_face_wire`, `rebuild_face`. 3 model author plus the separate LLM semantic reviewer. 4 production route: the FEA ladder's MESH-2 refusal hands its faces back to repair, then re-meshes.

## Global Constraints

- Every new behaviour is behind `CAD_REPAIR_MESH_PROBE`, a posture: `"live"` turns it on, any other value reads `"off"`. Off must be byte-for-byte today's behaviour; the existing scan-baseline tests must pass unchanged.
- `within=None` stays the exact `_localize_scan(shape, face_scan_s=budget)` call (`session.py:264`); mesh defects are appended after it, never passed into it.
- The probe NEVER raises and never blocks past its budget: every failure inside it becomes a `mesh_timeout` defect or an empty result (pattern: `localize._bounded`).
- gmsh runs only in a child process. It is never imported at module top level in `cad/repair/`, so hermetic unit tests run without gmsh.
- The in-loop probe imports a BREP the shape was written to, never STEP and never an OCC pointer: gmsh bundles its own OpenCASCADE, so passing OCP's in-memory shape across is an ABI hazard.
- Probe settings, verbatim from the corpus build: `General.NumThreads = 1`, `Mesh.MeshSizeMax = diagonal / 40`, `generate(2)` then `generate(3)`; a probe passes when it yields more than zero volume elements. Default budget 120 s; `CAD_REPAIR_MESH_PROBE_TIMEOUT_S` overrides.
- New defect codes are severity `error`, repairable (triage `_REPAIRABLE`), never reserved for a person.
- No corpus CAD is committed. The track manifest carries names, sha256, source URL and the gmsh error only (ABC models are their creators' copyright).
- Unit tests go in `tests/unit/cad/test_repair_*.py` and `tests/unit/dev/`; anything that runs gmsh or OCP goes in `tests/native/test_native_repair_*.py`.

## Review Focus

1. **A gmsh surface tag that matches no OCC face, or two.** Expect: reported unlocated (empty fingerprint), never aimed at the wrong face. Pinned in Task 2 (`test_ambiguous_tag_stays_unlocated`).
2. **gmsh hangs or segfaults** (3 of the OCCT files crashed with signal 11). Expect: a `mesh_timeout` defect, the worker unharmed, the run continues. Pinned in Task 1 (`test_probe_survives_child_crash`).
3. **A part that meshes before and after repair.** Expect: no mesh defects at all, and the flag-on report identical to flag-off apart from timing. Pinned in Task 5 (`test_sound_part_adds_no_mesh_defects`).
4. **The probe's own budget runs out mid-scan in a scoped region.** Expect: `mesh_timeout` in that region, which the predicate cannot falsely clear. Pinned in Task 6 (`test_timeout_region_cannot_claim_meshes`).
5. **A repair that makes a meshing part stop meshing.** Expect: counted as `newly_broken` in the corpus summary, never hidden in a net figure. Pinned in Task 4 (`test_newly_broken_is_counted_separately`).

---

### Task 1: The probe and its error classifier

**Files:**
- Create: `src/meshpipeline/cad/repair/mesh_probe.py`
- Test: `tests/unit/cad/test_repair_mesh_probe.py`, `tests/native/test_native_repair_mesh_probe.py`

**Interfaces:**
- Produces:
  - Kind constants: `OVERLAPPING_FACETS = "overlapping_facets"`, `EDGE_THROUGH_FACE = "edge_through_face"`, `OPEN_LOOP = "open_loop"`, `PERIODIC_SURFACE = "periodic_surface"`, `NO_VOLUME = "no_volume"`, `TIMEOUT = "timeout"`, `OTHER = "other"`.
  - `@dataclass(frozen=True, slots=True) class MeshFailure: kind: str; surfaces: tuple[int, ...]; point: tuple[float, float, float] | None; message: str`
  - `@dataclass(frozen=True, slots=True) class ProbeResult: meshed: bool; volume_elements: int; failure: MeshFailure | None; tag_boxes: dict[int, Box]`
  - `classify(error: str, log: Sequence[str]) -> MeshFailure` (pure)
  - `Box = tuple[float, float, float, float, float, float]` (xmin, ymin, zmin, xmax, ymax, zmax)
  - `probe_file(path: Path, *, diagonal_mm: float, surfaces: Sequence[int] | None = None, volume: bool = True, budget_s: float = 120.0) -> ProbeResult`: `surfaces=None` meshes everything; an empty sequence meshes nothing and only reads `tag_boxes`.

- [ ] **Step 1: Write the classifier tests**, using the exact strings the corpus produced:

```python
@pytest.mark.parametrize("error, kind, surfaces", [
    ("Invalid boundary mesh (overlapping facets) on surface 33 surface 33", OVERLAPPING_FACETS, (33,)),
    ("Invalid boundary mesh (overlapping facets) on surface 320 surface 324", OVERLAPPING_FACETS, (320, 324)),
    ("Impossible to mesh periodic surface 6", PERIODIC_SURFACE, (6,)),
    ("PLC Error:  A segment and a facet intersect at point (1.5,2,-3)", EDGE_THROUGH_FACE, ()),
    ("The 1D mesh seems not to be forming a closed loop", OPEN_LOOP, ()),
    ("something gmsh never said before", OTHER, ()),
])
def test_classify_names_kind_and_surfaces(error, kind, surfaces): ...
    # assert classify(error, []).kind == kind and .surfaces == surfaces

def test_plc_point_is_parsed():               # point == (1.5, 2.0, -3.0)
def test_open_loop_takes_surface_from_log():  # log [..., "Meshing surface 12 (Plane, Frontal-Delaunay)"] -> surfaces == (12,)
def test_open_loop_without_log_has_no_surface()
```

- [ ] **Step 2: Run** `pytest tests/unit/cad/test_repair_mesh_probe.py -v`. Expected: FAIL, module missing.

- [ ] **Step 3: Implement `classify`.** Regexes on `error`. For `OPEN_LOOP`, and for any kind whose error names no surface, take the tag from the last log line matching `Meshing surface (\d+)`. Unknown errors are `OTHER`, never raised.

- [ ] **Step 4: Run the unit tests.** Expected: PASS.

- [ ] **Step 5: Write the native tests** (gmsh and OCP available):

```python
def test_box_meshes(tmp_path):                 # BREP of a 10 mm box -> meshed True, volume_elements > 0, failure None
def test_overlapping_solids_fail_located(tmp_path):
    # compound of two interpenetrating boxes written to BREP -> meshed False, failure.kind == EDGE_THROUGH_FACE
def test_tag_boxes_cover_every_surface(tmp_path)   # len(result.tag_boxes) == number of OCC faces
def test_probe_survives_child_crash(monkeypatch, tmp_path):
    # make the child call os._exit(139) -> meshed False, failure.kind == TIMEOUT, no exception
def test_budget_is_honoured(tmp_path)          # budget_s=0.01 on the overlap fixture -> failure.kind == TIMEOUT, returns in < 2 s
```

- [ ] **Step 6: Implement `probe_file`.** The child body: `gmsh.initialize()`, terminal off, `General.NumThreads` 1, logger started, `occ.importShapes(str(path))`, `synchronize`, `MeshSizeMax = diagonal_mm / 40`. When `surfaces` is given, mesh only those (2D) and skip 3D. Record `getBoundingBox(2, tag)` for every surface into `tag_boxes`. `generate(2)`, then `generate(3)` when `volume`. Catch the gmsh exception and return `classify(str(exc), logger.get())`. `finalize` in `finally`. Run it through `localize._bounded(child, budget_s=budget_s)`; `None` back means `TIMEOUT`. Zero volume elements with no exception means `NO_VOLUME`.

- [ ] **Step 7: Run** `pytest tests/native/test_native_repair_mesh_probe.py -v`. Expected: PASS.

- [ ] **Step 8: Commit** `feat(repair): a gmsh probe that names why a part will not mesh`.

### Task 2: Mesh failures become located defects

**Files:**
- Modify: `src/meshpipeline/cad/repair/contracts.py:41-58` (DefectCode), `src/meshpipeline/cad/repair/triage.py:45-57` (`_REPAIRABLE`)
- Modify: `src/meshpipeline/cad/repair/mesh_probe.py`
- Test: `tests/unit/cad/test_repair_mesh_probe.py`, `tests/native/test_native_repair_mesh_probe.py`

**Interfaces:**
- Consumes: `probe_file`, `ProbeResult`, `MeshFailure` (Task 1); `localize._centroid_and_box`, `localize._stamped`, `localize.EntityDefect`, `regions.Region`, `localize.in_region`.
- Produces:
  - `DefectCode.mesh_overlapping_facets`, `mesh_edge_through_face`, `mesh_open_loop`, `mesh_periodic_surface`, `mesh_no_volume`, `mesh_timeout`, `mesh_other`. All seven are in `_REPAIRABLE`.
  - `CODE_FOR_KIND: dict[str, DefectCode]`, mapping Task 1's kinds one-to-one.
  - `faces_for_tags(face_boxes: dict[int, Box], tag_boxes: dict[int, Box], *, tol: float) -> dict[int, int]`, pure: gmsh tag to OCC 1-based face index.
  - `mesh_defects(shape, *, diagonal_mm: float, budget_s: float, within: Region | None = None) -> tuple[EntityDefect, ...]`

- [ ] **Step 1: Write the unit tests:**

```python
def test_faces_for_tags_matches_by_box()         # 3 distinct boxes, tags in a different order -> exact mapping
def test_ambiguous_tag_stays_unlocated()          # two OCC faces with the same box -> that tag absent from the result
def test_tag_outside_tolerance_is_unmapped()
def test_every_kind_has_a_code()                  # set(CODE_FOR_KIND) == every Task 1 kind
def test_mesh_codes_are_repairable_not_judgement()  # each mesh_* value in triage._REPAIRABLE, none in _NEEDS_JUDGEMENT
```

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement.**
  - Box matching: a tag maps to a face when every coordinate differs by at most `tol`, and exactly one face matches.
  - `mesh_defects`:
    1. Write `shape` to a temporary BREP with `BRepTools.Write_s`.
    2. Call `probe_file`. When `within` is given, pass `surfaces`: the tags whose mapped face passes `in_region`. That needs a first `probe_file(..., surfaces=[], volume=False)` call only to read `tag_boxes`; cache it per BREP path within the call.
    3. Map tags with `tol = diagonal_mm * 1e-6`.
    4. Emit one `EntityDefect(code=CODE_FOR_KIND[kind], severity=error, entity_type="face", entity_index=<face>)` per mapped surface. Its `message` is gmsh's text and its `location` comes from `_centroid_and_box`.
    5. A PLC point goes to the nearest face (`BRepExtrema_DistShapeShape` from a vertex at the point).
    6. A failure with no mapped face gives one defect with `entity_index=0` and an empty location. `mesh_no_volume` and `mesh_timeout` are always of that unlocated form.
    7. Pass everything through `_stamped(found, diagonal_mm=diagonal_mm)`.

- [ ] **Step 4: Native tests:**

```python
def test_overlap_fixture_defect_names_a_real_face()  # fingerprint non-empty and equal to localize's fingerprint for that face index
def test_mapping_agrees_with_occ_face_count_on_box()
def test_within_region_probes_only_region_faces()    # region around one box of the two -> no defect located outside it
```

- [ ] **Step 5: Run unit and native tests.** Expected: PASS. Then `make lint typecheck`.

- [ ] **Step 6: Commit** `feat(repair): mesh failures are located defects on named faces`.

### Task 3: The real-world track lands, calibrated

**Files:**
- Create: `devtools/validation/mesh_probe.py` (CLI)
- Create: `devtools/validation/corpora/mesh-failure-real.json`
- Create: `devtools/validation/evidence/2026-11-14-mesh-probe-calibration/README.md` plus the run's JSON
- Test: `tests/unit/dev/test_mesh_probe_cli.py`

**Interfaces:**
- Consumes: `probe_file`, `mesh_defects` (Tasks 1-2).
- Produces:
  - Manifest schema: `{"track": "mesh-failure-real", "probe": {...the Global Constraints settings...}, "sources": [{"name", "archive_url", "archive_sha256"}], "files": [{"name", "source", "sha256", "solids", "faces", "gmsh_error"}]}`
  - `load_manifest(path) -> dict`, `resolve_inputs(manifest, roots: list[Path]) -> list[Path]` (by sha256; a missing file is listed, never skipped silently)
  - `agreement(file_result: ProbeResult, loop_result: ProbeResult) -> bool` (both meshed, or both failed with the same kind)

- [ ] **Step 1: Write the manifest** from `~/archductor/mesh-fail-corpus/manifest.json`: the 80 non-synthetic entries, archive URLs and sha256 as recorded (OCCT `ee7eea66…`, ABC chunk 0050 `1855b6fa…`), and fetch steps in the README.

- [ ] **Step 2: Unit tests:** `test_resolve_by_hash_reports_missing`, `test_agreement_rules`, `test_manifest_has_80_files_and_no_cad`.

- [ ] **Step 3: Implement the CLI.** `python devtools/validation/mesh_probe.py --manifest … --roots DIR… --json OUT`. For each file it runs the file probe (STEP import) and the in-loop probe (read the STEP with OCP, then `mesh_defects` via BREP). It prints the agreement table and lists every disagreement.

- [ ] **Step 4: Run it on the 80 files.** Record agreement and disagreements in the evidence README. **Acceptance:** every file has a verdict, and every disagreement is written down with its cause or "unexplained". If more than 8 of 80 disagree, stop and report before Task 5: the in-loop probe would not be measuring what the track scores.

- [ ] **Step 5: Commit** `feat(validation): the mesh-failure-real track, with probe calibration evidence`.

### Task 4: The corpus runner scores meshing

**Files:**
- Modify: `devtools/validation/repair_corpus.py` (`_one`, `corpus_summary`, `main`)
- Test: `tests/unit/dev/test_repair_corpus_meshing.py`

**Interfaces:**
- Consumes: `probe_file` (Task 1).
- Produces:
  - Row keys, when `--mesh-probe` is passed: `meshes_before: bool`, `meshes_after: bool | None` (None when nothing was written), `mesh_kind_before: str`, `mesh_kind_after: str`.
  - `meshing_summary(rows: list[dict]) -> dict` with keys `files, meshed_before, meshed_after, newly_meshed, newly_broken, still_failing_by_kind`. Merged into `corpus_summary` under `"meshing"` only when any row carries the keys.

- [ ] **Step 1: Unit tests:** `test_newly_meshed_counts_fail_to_pass`, `test_newly_broken_is_counted_separately`, `test_unrepaired_file_keeps_its_before_verdict` (`meshes_after` None means `meshes_before` is counted for after), `test_summary_absent_without_probe`.

- [ ] **Step 2: Implement.** Probe the input file before repair, and the written `-repaired.step` after.

- [ ] **Step 3: Run** the unit tests and `pytest tests/unit/dev -q`. Expected: PASS, existing bundle tests unchanged.

- [ ] **Step 4: Commit** `feat(validation): the corpus runner reports which files mesh after repair`.

### Task 5: The repair session carries mesh defects, behind the posture

**Files:**
- Modify: `src/meshpipeline/settings/cad_repair.py` (and `settings/inventory.py`, `docs/reference/configuration.md` as the existing flags do)
- Modify: `src/meshpipeline/cad/repair/session.py:245-269` (`_default_scan`) and `:820-838` (the scoped scan)
- Test: `tests/unit/cad/test_repair_session_mesh.py`

**Interfaces:**
- Consumes: `mesh_defects` (Task 2).
- Produces:
  - `rcfg.CAD_REPAIR_MESH_PROBE: str` (`"live"` or `"off"`, the same fail-closed parse as `_region_loop_posture`)
  - `rcfg.CAD_REPAIR_MESH_PROBE_TIMEOUT_S: float` (default `120.0`)
  - `session._with_mesh(found, shape, *, within) -> tuple`: `found` plus `mesh_defects(...)` when live; `found` unchanged when off.

- [ ] **Step 1: Unit tests** (patch `mesh_probe.mesh_defects`):

```python
def test_off_never_calls_probe()                 # patched probe raises if called; full session.defects() still works
def test_typo_reads_off()                        # CAD_REPAIR_MESH_PROBE="Live " -> off
def test_live_appends_after_localize()           # order: localize's defects, then the mesh ones
def test_scoped_scan_passes_region()             # within=region is forwarded
def test_sound_part_adds_no_mesh_defects()       # probe returns () -> defects identical to off
def test_probe_failure_does_not_mark_scan_incomplete()
def test_per_operation_rescan_includes_mesh_defects()  # try_op's re-inspection (_rescan, face_scan=False) still appends them, so an op aimed at a mesh defect is judged by the progress rule
```

- [ ] **Step 2: Implement.** Use the original diagonal (`self.original.diagonal_mm`) on both scan paths. The budget is `min(CAD_REPAIR_MESH_PROBE_TIMEOUT_S, remaining)`.

- [ ] **Step 3: Run** the unit tests, `pytest tests/unit/cad -q`, and the native scan-baseline tier. Expected: all pass, and the byte-identical baseline still holds with the flag off.

- [ ] **Step 4: Commit** `feat(repair): the session sees mesh failures when CAD_REPAIR_MESH_PROBE is live`.

### Task 6: Packages can claim a face will mesh; the rules baseline aims at mesh defects

**Files:**
- Modify: `src/meshpipeline/cad/repair/rules_author.py`
- Test: `tests/unit/cad/test_repair_package.py`, `tests/unit/cad/test_repair_rules_author_mesh.py`

**Spec deviation, recorded:** the spec adds `Predicate.meshes`. Not needed: a mesh failure is a defect on a fingerprint, so "this face will mesh" is `cleared` naming that fingerprint, judged by the scoped re-scan Task 5 already enriches. `package.py` stays unchanged.

**Interfaces:**
- Consumes: mesh `DefectCode`s (Task 2), the session from Task 5.
- Produces: `rules_author.MESH_OPS: dict[DefectCode, str]`, the baseline's deterministic mapping: `mesh_open_loop -> "fix_face_boundary"`, `mesh_overlapping_facets -> "fix_shape_global"`, `mesh_edge_through_face -> "fix_shape_global"`. `mesh_periodic_surface`, `mesh_no_volume`, `mesh_timeout` and `mesh_other` map to nothing until Plan 2, so the author returns no package for them.

- [ ] **Step 1: Tests:**

```python
def test_cleared_mesh_fingerprint_fails_while_failure_remains()   # evaluate(...) with the fingerprint still in defects_after -> non-empty reason
def test_timeout_region_cannot_claim_meshes()     # region holds a mesh_timeout defect: author does not claim it cleared, and a package claiming it fails evaluate
def test_rules_author_maps_mesh_codes()           # each mapped code -> one-op plan with that op name, cleared == {fingerprint}
def test_unmapped_mesh_codes_get_no_package()
```

- [ ] **Step 2: Implement** the mapping in the author's existing code-to-op selection.

- [ ] **Step 3: Run** `pytest tests/unit/cad -q`. Expected: PASS.

- [ ] **Step 4: Commit** `feat(repair): the rules baseline aims existing operations at mesh failures`.

### Task 7: A run that ends unmeshable says so

**Files:**
- Modify: `src/meshpipeline/cad/repair/region_loop.py` (`_terminal` at `:207-223`, and the end of `run`)
- Modify: `src/meshpipeline/cad/repair/loop.py:28-38` (new constant), `src/meshpipeline/pipeline/repair_receipt.py` (`_STOPPED` near `:392`), `devtools/validation/repair_corpus.py` (stop-reason tallies)
- Test: `tests/unit/cad/test_repair_region_loop.py`, `tests/native/test_native_repair_region_loop.py`

**Interfaces:**
- Produces: `loop.UNMESHABLE = "unmeshable"`. `_terminal` gains `meshes: bool | None` (None means the probe is off or never ran). When it would return `RESOLVED` but `meshes is False`, it returns `UNMESHABLE`. `SCAN_INCOMPLETE` and `ESCALATED_REGIONS` keep precedence.

- [ ] **Step 1: Tests:**

```python
def test_resolved_but_failing_volume_mesh_is_unmeshable()
def test_probe_off_terminal_unchanged()           # meshes=None -> every existing case returns what it did
def test_incomplete_scan_still_wins()
def test_receipt_has_words_for_unmeshable()       # _STOPPED["unmeshable"] is non-empty plain English
```

- [ ] **Step 2: Implement.** When live, the end of `run` makes one whole-shape `probe_file` (volume on) on the final shape and passes `meshes=result.meshed` to `_terminal`. Committed packages stay; nothing is undone.

- [ ] **Step 3: Native:** `test_overlap_fixture_ends_unmeshable_not_resolved` (flag live, no operation can fix it, run stops `unmeshable`).

- [ ] **Step 4: Run** unit, native and `make check-fast`. Expected: PASS.

- [ ] **Step 5: Commit** `feat(repair): a run whose part still will not mesh ends unmeshable`.

### Task 8: Measure it on the track

**Files:**
- Create: `devtools/validation/evidence/2026-11-14-mesh-failure-location/README.md` plus sealed bundles

- [ ] **Step 1: Run** `repair_corpus.py --mesh-probe --bundle … --track mesh-failure-real` on the 80 files, region loop live, mesh probe `off` and then `live`, settings otherwise as the 2026-11-13 NIST evidence.

- [ ] **Step 2: Record**, with "what this does not show" at equal prominence:
  - files located (any `mesh_*` defect with a fingerprint), especially of the 15 the inspector saw nothing on;
  - `meshing_summary` for both arms;
  - `newly_broken`, which must be 0;
  - whole-shape and scoped scan counts, and probe seconds.

  **Acceptance:** at least 12 of the 15 previously invisible files are located, and `newly_broken == 0`. A small `newly_meshed` is expected: the operations that answer these families arrive in Plan 2.

- [ ] **Step 3: Commit** `Evidence: mesh failures located on the real-world track`.

### Task 9: The benchmark reaches inspector-clean failures (force-repair)

Added 2026-11-15 after Task 8 measured located 0/15: the 15 inspector-clean files never get a
repair session (triage routes a clean inspection to mesh-as-is), so the in-session probe cannot
locate them and Plans 2/3 cannot be scored on them. Production's answer is the Plan-4 handback;
the benchmark's answer is a runner-only switch. The customer path (triage) is untouched.

**Files:**
- Modify: `devtools/validation/repair_corpus.py`
- Test: `tests/unit/dev/test_repair_corpus_meshing.py`

**Interfaces:**
- Produces: with `--mesh-probe --force-repair`, a file whose probe fails before repair
  (`meshes_before is False`) runs the repair session even when triage routed it away; its row
  carries `forced_repair: true` and keeps the triage route it would have taken under
  `triage_route`. Without the flag, behaviour is byte-identical. `corpus_summary`'s meshing block
  gains `forced_repairs` (count). The bundle settings record `force_repair`.

- [ ] **Step 1: Failing unit tests** — `test_force_repair_runs_the_session_on_clean_inspections`
  (fake seams), `test_force_repair_rows_are_marked`, `test_without_the_flag_rows_are_unchanged`.
- [ ] **Step 2: Implement; PASS; lint; check-fast.**
- [ ] **Step 3: One-arm smoke** over 5 of the 15 files (probe live, force-repair): report how many
  now hold a located `mesh_*` fingerprint; no full-track re-run in this task.
- [ ] **Step 4: One targeted commit.**
