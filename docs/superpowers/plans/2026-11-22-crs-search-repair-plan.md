# CRS v4 Search Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a verifier-driven repair engine (deterministic staged healer, then a best-first search with an AI proposer over a parametric OCCT op library), judged by one clean-STEP checker, plus a benchmark rebuilt from inputs that fail that checker.

**Architecture:**
- A new package `meshpipeline.cad.repair_v4` sits beside v3 and is selected by `CAD_REPAIR_ENGINE=search`.
- All kernel work runs in a warm, killable worker pool.
- The `verdict` checker (valid → panel-meshable → faithful) is the search reward, the delivery gate and the benchmark scoreboard.
- The eval reuses the v3_eval fleet machinery with new arms.

**Tech Stack:** Python 3.12, OCP (OCCT 7.8) incl. XCAF/STEPCAFControl, gmsh 4.15.2, Netgen (`netgen-mesher`, OCC interface), pytest, the existing GCE eval fleet.

**Spec:** `docs/superpowers/specs/2026-11-22-crs-search-repair-design.md`. Supporting: `docs/superpowers/specs/crs-v4/`.

## Global Constraints

- **Off in prod.** `CAD_REPAIR_ENGINE ∈ {v3, search}`, default `v3`; production posture never sets it.
- **Clean = all three layers:**
  - **Valid.** Hard fails only:
    - BRepCheck invalid
    - open or non-manifold shell
    - non-positive volume
    - self-intersection
    - non-conformal body overlap
    - units changed
    - round trip not valid

    Small entities and tolerances are reported, not failing.
  - **Meshable.** Meshes on every panel member with min SICN ≥ 0.01. No hints, no fTetWild.
  - **Faithful.** Two-sided distance ≤ min(0.1 mm, 0.001 × diagonal), with these exceptions:
    - One-sided original → repaired for declared added faces.
    - Declared removals are exempt: duplicates, or slivers narrower than the bound.
    - Fills only for loops spanning ≤ 5% of the diagonal.
    - Area/volume change ≤ 2%.
    - Body count preserved.
- **Panel:** gmsh production FEA config; gmsh defaults; gmsh alternate (2D Frontal-Delaunay, 3D Delaunay); Netgen via OCC.
- **Multi-body parts:** meshed as one conformal mesh. Touching bodies must share faces after the STEP round trip. No `fuse` without an intent signal; `cut_by` only when the overlap volume is within the fidelity bound.
- **XCAF:** body/face names, colours and assembly structure are preserved through STEPCAFControl. PMI is carried, not checked.
- **Kernel isolation:** no kernel call in the parent process inside v4 code. Every call goes through `WorkerPool`: workers are spawned fresh (never forked), each call has a time limit, and a timed-out worker is SIGKILLed and replaced.
- **Regressions:**
  - any clean input made unclean;
  - any output passing fewer panel members than its input.
- **Delivery rule:** deliver the best state only if it passes ≥ as many panel members as the input; else pass the input through.
- **Eval budget:**
  - `search_ai` model ceiling $1.00/file;
  - arms compared at equal wall-clock and op budgets;
  - sprint compute ≤ ~600 c2d VM-hours.
- **Merge gate per PR:**
  - unit tests;
  - `ruff` + `devtools/quality/mypy_ratchet.py`;
  - the native repair tier run locally (`make test-native-all` or the repair subset) with no new failures;
  - wave results attached when the PR changes repair behaviour.
- **Held-out split** is never used for tuning or for recipe memory.

## Testing and Cost Policy (binding on every lane)

Test in tiers, and spend money only at the outermost one.

| Tier | What runs | Where | When | Cost |
|---|---|---|---|---|
| T0 | Unit tests with fake pool, fake verdict, scripted fake model | laptop | every change | free |
| T1 | Native repair tier (real OCCT/gmsh/Netgen) | local mesh Docker image | before every merge | free |
| T2 | **Smoke slice**: 30 dev files, stratified, frozen list. All deterministic arms. `search_ai` on 10 of them at $0.50/file | laptop, or 1 spot VM | per PR that changes repair behaviour | < $5 |
| T3 | Dev wave | spot c2d fleet, self-deleting | deterministic arms whenever useful; `search_ai` on a **rotating 100-file dev subset**, with the full 300 only at checkpoints (≤ 3 times) | VM ~$1–5 per wave; model hard cap **$50 per wave** (checkpoint waves $300) |
| T4 | Held-out + clean control | fleet | **once**, at a frozen commit | model hard cap $250 |

**Cost controls (must be implemented, not just followed):**
- **Verdict cache.** Key: (STEP sha256, panel config hash, verdict code version). Stored in the eval bucket and read before any mesher runs. Unchanged inputs and states are never re-meshed. The re-probe runs once, and each arm reuses it as `input_only`.
- **Ledger and hard cap.** Every model call is written to a per-wave ledger. `launch_fleet`/`run_arm` refuse to start without `--model-usd-cap`, and each shard stops calling the model once the shared ledger reaches the cap; remaining files run deterministic-only and are marked `ai_capped`.
- **Cheap model hygiene.**
  - Prompt caching on the static system prompt and op schemas.
  - Images only for sites whose kind needs an intent call (overlap, open loop/fill).
  - Stop a file the moment it is clean.
  - Per-file $ ceiling from the Global Constraints.
- **Compute.**
  - Spot VMs that self-delete with a 3 h backstop.
  - The cheap tier is used to rank; the full panel runs only on the top-K states.
  - The launcher preflight prints the projected VM-hours and refuses above the sprint ceiling (~600 c2d VM-hours) without an override flag.
- **Sprint estimate:** VM ~$50–100 for the whole sprint (re-probe included); model ~$150–350, dominated by 2–3 checkpoint waves and the held-out run.

Owner tasks for these controls: the verdict cache goes in Task 5, the ledger and cap in Tasks 19/21, the preflight in Task 8.

## Review Focus

1. **Touching multi-body part after a STEP round trip** comes back with coincident duplicate faces. Expect: the checker and `conformalise` handle it, and the part is not reported as a false overlap. Test in Task 3 and Task 12.
2. **STEP in inches or metres.** Expect: units are preserved and the deviation bound is computed in mm. Test in Task 2 and Task 5.
3. **Surface-only STEP or empty file (no solids).** Expect: a verdict of `valid=False, reason="no_solids"`, no crash, and the file is excluded from the benchmark failing set. Test in Task 2 and Task 8.
4. **A panel mesher segfaults or hangs.** Expect: that member is recorded as `crashed`/`timeout`, the worker is replaced, and the remaining members still run. Test in Task 1 and Task 3.
5. **The AI proposes a target ID that a previous op removed.** Expect: a structured schema error back to the model, not a refusal streak and not a crash. Test in Task 9 and Task 19.

---

## File Structure

```
src/meshpipeline/cad/repair_v4/
  __init__.py            # engine entry: repair_v4(source, dest, budget) -> V4Result
  worker_pool.py         # Task 1: warm killable kernel workers
  kernel_calls.py        # Task 1: allow-listed kernel functions run inside workers
  xcaf_io.py             # Task 2: STEPCAFControl read/write preserving labels
  verdict/
    __init__.py          # Task 5: verdict(), cheap_verdict(), Verdict, score()
    valid.py             # Task 2: layer 1
    panel.py             # Task 3/4: PanelMember protocol + runner
    gmsh_members.py      # Task 3: prod / default / alt
    netgen_member.py     # Task 4
    faithful.py          # Task 5: layer 3
  workspace.py           # Task 9: Workspace, StableId, Diff
  types.py               # Task 9: Site dataclass (shape per Task 17)
  ops/
    __init__.py          # Task 11: Op protocol, registry
    split.py             # Task 11: split_by_proximity, split_angle, split_closed, split_continuity, unify_same_domain
    bodies.py            # Task 12: conformalise, remove_duplicate_faces
    edges.py             # Task 13: rebuild_edge_as_intersection, tighten_tolerances, fix_poles
    faces.py             # Task 14: rebuild_face, fill_loop, reapproximate, to_canonical, refit_cluster
    shells.py            # Task 15: remove_small, sew, make_volume, orient_and_close
  stages.py              # Task 16: staged healer
  sites.py               # Task 17: failure sites + observations
  render.py              # Task 17: site views (in worker)
  search.py              # Task 18: best-first driver
  proposer.py            # Task 19: AI proposer
  trace.py               # Task 18: per-file trace record
src/meshpipeline/cad/repair/conservative.py   # Task 20: dispatch on CAD_REPAIR_ENGINE
src/meshpipeline/cad/repair/model_author.py   # Task 10: clone/delivery parity (v3 gate fix)
src/meshpipeline/settings/cad_repair.py       # Task 20
src/meshpipeline/settings/inventory.py        # Task 20
docs/reference/configuration.md               # Task 20
deploy/docker/ (mesh image)                   # Task 4: netgen-mesher
devtools/validation/v4_eval/
  pool.py                # Task 6: source fetchers + manifests
  inject.py              # Task 7: defect injectors
  reprobe.py             # Task 8: verdict on candidate inputs, split builder
  evalspec.py            # Task 21: arms
  run_arm.py, collect.py # Task 8/21 (adapt v3_eval; per-file wall kill)
devtools/validation/corpora/v4/{pool,dev,heldout,clean}.json
tests/unit/cad/repair_v4/...   tests/native/test_native_repair_v4_*.py
```

---

## Lane L0: checker

### Task 1: Warm killable worker pool

**Files:**
- Create: `src/meshpipeline/cad/repair_v4/worker_pool.py`, `kernel_calls.py`
- Test: `tests/unit/cad/repair_v4/test_worker_pool.py`

**Interfaces:**
- Produces:
  - `WorkerPool(size: int, *, start_timeout_s: float = 30)` with `call(fn_name: str, payload: dict, *, timeout_s: float) -> CallResult` and `close()`.
  - `CallResult(ok: bool, value: dict | None, error: str | None, status: Literal["ok","error","timeout","crashed"], seconds: float)`.
  - Shapes cross as BREP file paths inside `payload`/`value`.
  - `kernel_calls.REGISTRY: dict[str, Callable[[dict], dict]]`. Only names in it are callable.

- [ ] **Step 1: Write failing tests**
  - `test_call_returns_value` (echo fn).
  - `test_timeout_kills_and_replaces`: a sleep-forever fn gives `status=="timeout"`; the next call succeeds; the worker pid changed.
  - `test_crash_is_reported`: an `os.abort` fn gives `status=="crashed"`; the pool stays usable.
  - `test_unknown_fn_refused`: gives `status=="error"`.
  - `test_workers_are_spawned_not_forked`: the start method is `"spawn"`.
- [ ] **Step 2:** Run `pytest tests/unit/cad/repair_v4/test_worker_pool.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement with `multiprocessing.get_context("spawn")` and one pipe per worker. On timeout: `os.kill(pid, SIGKILL)` then respawn. Workers import OCP once at start. Test-only fns are registered behind `HEXERA_TEST_KERNEL_FNS=1`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): warm killable kernel worker pool`.

### Task 2: XCAF I/O and layer 1 (valid)

**Files:**
- Create: `xcaf_io.py`, `verdict/valid.py`
- Add kernel fns: `read_step_xcaf`, `write_step_xcaf`, `check_valid`
- Test: `tests/unit/cad/repair_v4/test_valid.py`, `tests/native/test_native_repair_v4_valid.py`

**Interfaces:**
- Consumes: `WorkerPool`.
- Produces:
  - `ValidReport(ok: bool, hard: list[Finding], soft: list[Finding], units: str, solids: int, round_trip_ok: bool)`.
  - `Finding(code: str, entities: list[str], detail: dict)`. Hard codes: `brep_invalid`, `open_shell`, `non_manifold`, `non_positive_volume`, `self_intersection`, `body_overlap`, `units_changed`, `round_trip_invalid`, `no_solids`. Soft codes: `small_edge`, `small_face`, `large_tolerance`.
  - `check_valid(pool, step_path, *, original_units: str | None) -> ValidReport`.
  - `xcaf_io.read(path) -> XcafDoc` / `write(doc, path)`. `XcafDoc` holds the shape BREP path plus a label map `{stable_id: {"name","color","parent"}}`.

- [ ] **Step 1:** Write failing native tests on fixtures built in-test:
  - valid box → `ok`;
  - box with one face removed → `open_shell`;
  - two overlapping boxes → `body_overlap`;
  - two boxes sharing a face written via XCAF compound → `ok` (Review Focus 1);
  - inch STEP → `units=="inch"` and no `units_changed` (Review Focus 2);
  - surface-only STEP → `no_solids`, `ok=False` (Review Focus 3);
  - names survive read → write → read.
- [ ] **Step 2:** Run `pytest tests/native/test_native_repair_v4_valid.py -v` inside the mesh image (`make test-native-all` subset). Expected: FAIL.
- [ ] **Step 3:** Implement with BRepCheck_Analyzer, ShapeAnalysis_FreeBounds, ShapeAnalysis_ShapeContents, BOPAlgo_ArgumentAnalyzer (self-interference on), pairwise BRepAlgoAPI_Common volume > 1e-9·V on non-face-sharing pairs, BRepGProp volume sign, and a round trip through `write_step_xcaf` → `read_step_xcaf` → re-check.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): XCAF IO and validity layer`.

### Task 3: Panel runner and gmsh members

**Files:**
- Create: `verdict/panel.py`, `verdict/gmsh_members.py`
- Create: `src/meshpipeline/engines/fea_mesh_config.py`, extracting the production FEA gmsh options from `engines/gmsh/fea_adapter.py:1511-1660` and `engines/fea_plan.py:452` into a pure function; `fea_adapter` then uses it, behaviour unchanged.
- Test: `tests/unit/cad/repair_v4/test_panel.py`, `tests/unit/engines/test_fea_mesh_config.py`, `tests/native/test_native_repair_v4_panel.py`

**Interfaces:**
- Produces:
  - `PanelMember` protocol: `name: str`, `run(pool, step_path, *, timeout_s) -> MemberResult`.
  - `MemberResult(name, status: Literal["pass","fail","timeout","crashed"], error_kind: str | None, message: str, located: list[Located], min_sicn: float | None, elements: int, seconds: float)`.
  - `Located(entity: str | None, xyz: tuple[float,float,float] | None, mesher_tag: str)`.
  - `run_panel(pool, step_path, members, *, timeout_s) -> list[MemberResult]`.
  - Members: `GmshProd`, `GmshDefault`, `GmshAlt`.
  - `fea_mesh_config.options(diag_m: float) -> dict[str, float|int]`.
- **Pass:** status `pass` requires a valid tet mesh (no inverted elements) and `min_sicn ≥ 0.01`.

- [ ] **Step 1:** Write failing tests.
  - `test_fea_mesh_config_matches_adapter`: snapshot of the option dict for diag 1.0 equals the values the adapter set before the refactor.
  - Native: box passes all three.
  - Native: a known ABC overlapping-facets file (from the `mesh-failure-real.json` manifest) fails `GmshDefault` with `error_kind=="overlapping_facets"` and non-empty `located`.
  - `test_member_crash_isolated` (Review Focus 4): a member forced to abort reports `crashed` and the others still run.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Members run gmsh inside worker kernel fns (`mesh_gmsh(step, options) -> dict`). Map surface tags to stable IDs by fingerprint (reuse `cad/repair/identity.py`), and error kinds with `cad/repair/mesh_probe.classify`. SICN via `gmsh.model.mesh.getElementQualities(..., "minSICN")`.
- [ ] **Step 4:** Run. Expected: PASS. Also run `pytest tests/unit/engines -q` to confirm no FEA regressions.
- [ ] **Step 5:** Commit `feat(repair_v4): mesher panel with gmsh members; extract prod FEA config`.

### Task 4: Netgen panel member

**Files:**
- Modify: the mesh image Dockerfile under `deploy/` (add pinned `netgen-mesher`), and `pyproject.toml` optional extra `repair_v4`.
- Create: `verdict/netgen_member.py`
- Test: `tests/native/test_native_repair_v4_netgen.py`

**Interfaces:**
- Produces: `NetgenOCC` implementing `PanelMember`. Its `min_sicn` comes from re-reading the Netgen volume mesh into gmsh and running `getElementQualities`.

- [ ] **Step 1:** Write failing native tests: box → `pass`; two face-sharing boxes → `pass` as one conformal mesh (glued compound); the overlapping-facets ABC file reports `fail` or `pass` with a recorded message, never an exception.
- [ ] **Step 2:** Run. Expected: FAIL (module/dependency missing).
- [ ] **Step 3:** Implement with `netgen.occ.OCCGeometry(step).GenerateMesh(maxh=diag/10)` inside a worker kernel fn. Export to `.vol`/`.msh`, then gmsh quality. Pin the netgen version in the image.
- [ ] **Step 4:** Run `make test-native-smoke` (the image builds and netgen imports) plus the new test. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): Netgen panel member`.
- **Fallback (spec §9):** if integration exceeds a day, ship the panel with three gmsh members, label results `panel=3`, and keep this task open.

### Task 5: Layer 3, Verdict assembly, cheap tier, score

**Files:**
- Create: `verdict/faithful.py`, `verdict/__init__.py`
- Test: `tests/unit/cad/repair_v4/test_verdict_score.py`, `tests/native/test_native_repair_v4_faithful.py`

**Interfaces:**
- Consumes: Tasks 2–4. `cad/repair/deviation.py` sampling logic, ported into a kernel fn.
- Produces:
  - `FaithfulReport(ok, max_dev_mm, bound_mm, area_ratio, volume_ratio, bodies_before, bodies_after, declared_added: list[str], declared_removed: list[str])`.
  - `check_faithful(pool, original, repaired, *, declared_added, declared_removed) -> FaithfulReport`.
  - `Verdict(valid: ValidReport, panel: list[MemberResult], faithful: FaithfulReport | None, clean: bool, tier: Literal["cheap","full"])`.
  - `verdict(pool, step, original, *, declared=Declared()) -> Verdict` (full panel).
  - `cheap_verdict(...)`: layer 1 + `GmshDefault` only.
  - `score(v: Verdict) -> tuple[int, int, float, float, int]` = (valid, panel passes, min SICN over members, −max_dev_mm, −topology churn).
  - `Declared(added: list[str] = [], removed: list[str] = [])`.

- [ ] **Step 1:** Write failing tests.
  - `test_score_orders_valid_first`.
  - `test_score_counts_panel_passes`.
  - Native: a box translated 0.2 mm on a 100 mm part → `faithful.ok is False`.
  - Native: a planar fill of a 2 mm hole declared added → one-sided, `ok`.
  - Native: an inch part with 0.003 in deviation → computed in mm (0.0762), `ok`.
  - `test_clean_requires_all_layers`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Bound = `min(0.1, 0.001*diag_mm)`. Two-sided sampling, excluding declared added faces from the repaired→original direction and declared removed faces from the original→repaired direction. Fills are allowed only if the loop span is ≤ 0.05·diag (otherwise `ok=False, reason="fill_too_large"`).
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): faithful layer, Verdict, cheap tier, score`.

---

## Lane L1: benchmark

### Task 6: Candidate pool sources

**Files:**
- Create: `devtools/validation/v4_eval/pool.py`, `devtools/validation/corpora/v4/pool.json`
- Test: `tests/unit/dev/test_v4_pool.py`

**Interfaces:**
- Produces:
  - `PoolEntry(name, source: Literal["abc_fail","abc_pass","occt","nist","f360","inject"], sha256, url, archive_sha256 | None, licence: str)`.
  - `build_pool(sources) -> list[PoolEntry]`, written to `pool.json`.
- **Sources:** ABC entries come from the existing `mesh-failure-real.json` and `evidence/2026-11-19-corpus-harvest/clean-samples.json`. NIST MBE PMI models come from the NIST download page (all translator variants kept, grouped by part). Fusion 360 Gallery comes from the segmentation-extended STEP set.

- [ ] **Step 1:** Write failing tests: `test_pool_dedupes_by_sha`, `test_pool_records_licence`, `test_nist_variants_grouped` (same part id → `group` field).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the fetchers (download to the eval bucket, record sha256). No CAD is committed, only names, hashes and URLs.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(v4_eval): candidate pool manifest`.

### Task 7: Defect injectors

**Files:**
- Create: `devtools/validation/v4_eval/inject.py`
- Test: `tests/native/test_native_v4_inject.py`

**Interfaces:**
- Produces: `inject(src_step, kind, seed) -> InjectedCase(path, kind, label: dict)`.
- Kinds: `loose_reapprox`, `unsew_perturb`, `tolerance_inflate`, `pcurve_corrupt`, `seam_variant`, `small_entities`, `face_delete`, `assembly_overlap`, `precision_truncate`, `iges_roundtrip`.
- `calibrate(cases, target_hist) -> dict[kind, weight]` matches the gmsh failure-family histogram of `mesh-failure-real.json`.

- [ ] **Step 1:** Write failing native tests: each kind applied to a clean box-with-fillets fixture produces a file whose `cheap_verdict` differs from the source's, and whose `label` names the injected entities.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement each kind with the OCCT calls listed in `03-healing-research-summary.md` (data section).
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(v4_eval): defect injectors`.

### Task 8: Re-probe and splits

**Files:**
- Create: `devtools/validation/v4_eval/reprobe.py`, `run_arm.py`, `collect.py` (adapt from `devtools/validation/v3_eval/`)
- Modify: shared harness per-file isolation (`devtools/validation/repair_corpus.py:2072-2130`) to add a wall-clock kill.
- Test: `tests/unit/dev/test_v4_reprobe.py`, `tests/unit/dev/test_repair_corpus_isolation.py`

**Interfaces:**
- Produces:
  - `reprobe(entries, *, tier) -> list[ProbeRow]`, where `ProbeRow(name, sha256, source, verdict_summary: dict, first_failing_layer: Literal[None,"valid","panel","faithful"], family: str)`.
  - `build_splits(rows, *, dev=300, heldout=200, clean=150, seed=20261122) -> Splits`, stratified by (first_failing_layer, family, source). It writes `corpora/v4/{dev,heldout,clean}.json`.
- **Re-probe order:** cheap tier on the whole pool. Then the full panel on cheap-tier failures plus a 10% sample of passes. Cheap/full agreement goes into the report.

- [ ] **Step 1:** Write failing tests.
  - `test_no_solids_excluded_from_failing` (Review Focus 3).
  - `test_splits_disjoint_and_stratified`.
  - `test_heldout_never_in_dev`.
  - `test_per_file_wall_kill`: a hung child is killed after `--file-timeout-s` and the row is `outcome="timeout"`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Reuse `launch_fleet.py` unchanged; `run_arm` calls `reprobe` per shard. Replace the `waitpid(pid, 0)` loop with a polling wait plus SIGKILL at the timeout.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit, then **launch the re-probe on the fleet the same day**. Owner compute ceiling: ~600 c2d VM-hours for the sprint.

---

## Lane L2: workspace, plus the v3 gate fix

### Task 9: Workspace, stable IDs, Diff

**Files:**
- Create: `workspace.py`, `types.py` (holds the `Site` dataclass defined in Task 17's Interfaces block, so the ops in Task 11 can import it before Task 17 fills in detection)
- Test: `tests/unit/cad/repair_v4/test_workspace.py`, `tests/native/test_native_repair_v4_workspace.py`

**Interfaces:**
- Consumes: `WorkerPool`, `xcaf_io`, `cad/repair/identity.fingerprint_of`.
- Produces:
  - `StableId = str` (e.g. `"F41"`, `"E7"`, `"B2"`).
  - `Workspace(brep_path: str, ids: dict[StableId, str], labels: dict, history: list[Diff], original: str)`.
  - `Workspace.fork() -> Workspace`.
  - `Workspace.resolve(ids: list[StableId]) -> list[StableId]` raises `UnknownTarget(id, reason)` for removed or unknown IDs.
  - `Diff(op: str, params: dict, modified: dict[StableId, list[StableId]], generated: dict[StableId, list[StableId]], removed: list[StableId], declared_added: list[StableId], declared_removed: list[StableId], seconds: float)`.
  - `apply_history(ws, history_json) -> Workspace`: the new ID map is built from the BRepTools_History record a kernel fn returns.

- [ ] **Step 1:** Write failing tests.
  - Native: split one face of a box → the old ID maps to two generated IDs and the names propagate.
  - `test_resolve_removed_id_raises_unknown_target` (Review Focus 5).
  - `test_fork_is_independent`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Fresh IDs are allocated monotonically per workspace lineage; labels are inherited by `Generated` children.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): workspace with stable ids and diffs`.

### Task 10: v3 clone/delivery gate parity (early v3 number)

**Files:**
- Modify: `src/meshpipeline/cad/repair/model_author.py:420-452` (`plan_entry` / `validate_package`)
- Test: `tests/unit/cad/test_repair_model_author_clone_parity.py`

**Interfaces:**
- Produces: `validate_package(..., cured_on_clone: bool)`. When the clone recorded `meshes_after_target_remains` for the plan's edits, the op-answers-defect table is not applied. All other delivery gates are unchanged.

- [ ] **Step 1:** Write failing test `test_clone_cured_package_not_refused_op_does_not_answer`. A package of `split_closed_faces` against `mesh_overlapping_facets`, with a clone cure recorded, validates. The same package without a cure is still refused.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the flag pass-through from investigator conclude (`investigator.py:1440`).
- [ ] **Step 4:** Run, plus `pytest tests/unit/cad -q -k repair`. Expected: PASS.
- [ ] **Step 5:** Commit `fix(repair): a package the clone proved meshing is not refused by the catalogue table`. Open a PR; run one v3 `meshability_pass_ai` wave on the **old** 120 sample for the before/after number.

---

## Lanes L3/L4: op library

### Task 11: Op protocol, registry, split ops

**Files:**
- Create: `ops/__init__.py`, `ops/split.py`; add kernel fns
- Test: `tests/unit/cad/repair_v4/test_ops_registry.py`, `tests/native/test_native_repair_v4_ops_split.py`

**Interfaces:**
- Consumes: Workspace (Task 9), the `cad/repair/gap_split.py` algorithm (port its `partners`/`split_plan`/`iso_edges`/`_split_body` into kernel fns).
- Produces:
  - `Op` protocol: `name`, `params_schema: dict` (JSON Schema with bounds), `applies_to(site: Site) -> bool`, `apply(pool, ws, targets: list[StableId], params: dict, *, timeout_s) -> tuple[Workspace, Diff]`.
  - `REGISTRY: dict[str, Op]`.
  - `validate_call(name, targets, params, ws) -> list[str]` returns schema/target errors.
  - Ops in this task:
    - `split_by_proximity(face_pairs, target_ratio∈[0.05,1])`
    - `split_angle(faces, max_deg∈[30,180])`
    - `split_closed(faces)`
    - `split_continuity(faces, order∈{1,2})`
    - `unify_same_domain(scope, keep: list[StableId])`

- [ ] **Step 1:** Write failing tests.
  - Each op on its fixture: a thin-gap slab for proximity (the cheap verdict goes from fail to pass, deviation 0); a full cylinder for angle/closed; a C0 B-spline for continuity.
  - `unify_same_domain` with `keep` does not merge kept split edges.
  - `test_validate_call_rejects_out_of_bounds`.
  - `test_validate_call_rejects_removed_target`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with ShapeUpgrade_ShapeDivideAngle, ShapeDivideClosed, ShapeDivideClosedEdges, ShapeDivideContinuity, ShapeUpgrade_UnifySameDomain (KeepShape) and the ported gap split (generalised from one face to face pairs).
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): op registry and split ops`.

### Task 12: Body ops

**Files:** Create `ops/bodies.py`. Test `tests/native/test_native_repair_v4_ops_bodies.py`

**Interfaces:**
- Produces:
  - `conformalise(bodies, fuzzy_mm∈[0, bound], mode∈{"fragment","cut_by:<B>"})`. `fuse` is excluded per Global Constraints.
  - `remove_duplicate_faces(pairs)` declares its removals in the Diff.

- [ ] **Step 1:** Write failing tests.
  - Two boxes touching (Review Focus 1): after `conformalise(fragment)` → one compound with a shared face; the XCAF round trip keeps it shared; the full verdict is valid.
  - Two boxes overlapping 0.05 mm on a 100 mm part, `cut_by` → valid and faithful.
  - Overlapping 5 mm, `cut_by` → op returns `declined: exceeds_fidelity`.
  - Duplicate coincident face → removed and declared.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with BOPAlgo_Builder (SetFuzzyValue, SetGlue(BOPAlgo_GlueShift), SetNonDestructive) and BOPAlgo_CellsBuilder.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): conformalise and duplicate-face ops`.

### Task 13: Edge and tolerance ops

**Files:** Create `ops/edges.py`. Test `tests/native/test_native_repair_v4_ops_edges.py`

**Interfaces:**
- Produces:
  - `rebuild_edge_as_intersection(edges)`
  - `tighten_tolerances(scope, target_rel∈[1e-7,1e-4])`
  - `fix_poles(faces)`

- [ ] **Step 1:** Write failing tests: an edge with tolerance inflated to 1e-2 on a 100 mm box (built with the `tolerance_inflate` injector from Task 7) → after both ops the max edge tolerance is ≤ 1e-5·diag and the cheap verdict improves; a sphere with a missing degenerate edge → `fix_poles` gives a valid result.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with GeomAPI_IntSS + BRepBuilderAPI_MakeEdge on the face pair, ShapeFix_Edge (FixAddPCurve, FixSameParameter), BRepLib::BuildCurves3d, ShapeFix_ShapeTolerance::LimitTolerance, ShapeFix_Face / ShapeFix_Wire::FixDegenerated.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): edge and tolerance ops`.

### Task 14: Face rebuild ops

**Files:** Create `ops/faces.py`. Test `tests/native/test_native_repair_v4_ops_faces.py`

**Interfaces:**
- Produces:
  - `rebuild_face(face, continuity∈{"G0","G1"})`
  - `fill_loop(loop_edges, continuity)`: declares the added face; refused if span > 0.05·diag.
  - `reapproximate(faces, tol_mm ≤ bound, max_degree∈[3,9])`
  - `to_canonical(faces, tol_mm ≤ bound)`
  - `refit_cluster(faces, tol_mm ≤ bound)`: declares one added face and the cluster removed.

- [ ] **Step 1:** Write failing tests: a self-intersecting B-spline face fixture → `rebuild_face` valid and within bound; a 2 mm hole → `fill_loop` gives a clean, one-sided faithful result; a 20% span hole → refused; a near-plane B-spline → `to_canonical` gives a plane within 1e-4 mm.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with ShapeFix_Wire::FixSelfIntersection, then BRepOffsetAPI_MakeFilling; BRepBuilderAPI_MakeFace for planar loops; ShapeCustom_BSplineRestriction; ShapeAnalysis_CanonicalRecognition; GeomAPI_PointsToBSplineSurface with boundary constraints.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): face rebuild, fill and refit ops`.

### Task 15: Small entities, sewing, volume, orientation

**Files:** Create `ops/shells.py`. Test `tests/native/test_native_repair_v4_ops_shells.py`

**Interfaces:**
- Produces:
  - `remove_small(entities, max_width_mm ≤ bound)`: declares removals.
  - `sew(tol_ladder: list[float] ⊆ [1e-6·diag, bound])`
  - `make_volume(faces)`
  - `orient_and_close()`

- [ ] **Step 1:** Write failing tests: a box shelled into 6 loose faces with 1e-4 gaps → `sew` + `make_volume` gives a valid solid; an inside-out solid → `orient_and_close` gives positive volume; a 0.01 mm sliver face → removed and declared, faithful.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with ShapeFix_Wireframe::FixSmallEdges, ShapeFix_FixSmallFace, BRepAlgoAPI_Defeaturing, BRepBuilderAPI_Sewing, BOPAlgo_MakerVolume, ShapeFix_Shell, ShapeFix_Solid, BRepLib::OrientClosedSolid.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): small-entity, sewing, volume and orientation ops`.

---

## Lane L5: staged healer

### Task 16: Staged deterministic healer

**Files:** Create `stages.py`. Test `tests/unit/cad/repair_v4/test_stages.py`, `tests/native/test_native_repair_v4_stages.py`

**Interfaces:**
- Consumes: ops (Tasks 11–15), `cheap_verdict`, `score`.
- Produces:
  - `STAGES: list[Stage]` in order: baseline, normalise, edges, faces, shells, solids, bodies, simplify, condition, finalise.
  - `Stage(name, run(pool, ws, params) -> tuple[Workspace, list[Diff]])`.
  - `run_staged(pool, ws, *, params: dict[str, dict] = {}) -> StagedResult(ws, kept: list[str], rolled_back: list[str], verdicts: dict[str, tuple])`.
  - `run_stage(pool, ws, name, params)` for the search's macro action.
- **Transaction rule:** apply on a fork, run `cheap_verdict`, keep only if `score(after) >= score(before)`.

- [ ] **Step 1:** Write failing tests: `test_stage_rolled_back_when_score_drops` (a stub stage that breaks validity); `test_order_matches_spec`; native: an injected `unsew_perturb` box comes back clean after `run_staged`; native: a clean control part is unchanged in score.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. The default parameters per stage come from `03-healing-research-summary.md` (stage order list). The condition stage runs angle (90°), closed, continuity (C1) and proximity (ratio 0.25) splits; simplify runs before condition, with `unify_same_domain` keeping no split edges.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): staged deterministic healer`.

---

## Lanes L6–L8: sites, search, proposer

### Task 17: Failure sites and observations

**Files:** Create `sites.py`, `render.py`. Test `tests/unit/cad/repair_v4/test_sites.py`, `tests/native/test_native_repair_v4_sites.py`

**Interfaces:**
- Consumes: `Verdict`, Workspace, `cad/repair/mesh_clash.py`, `occ_distance` (ported to kernel fns).
- Produces (`Site` lives in `types.py`, created in Task 9; this task implements detection):
  - `Site(id: str, kind: Literal["proximity","self_overlap","intersection","open","overlap","invalid","sliver","periodic","pole","timeout","unlocated"], entities: list[StableId], xyz: list[tuple], meshers_failing: list[str], dist_over_h: float | None, graph: dict, view_png: bytes | None)`.
  - `find_sites(pool, ws, verdict, *, h_mm) -> list[Site]`.
  - `observation(ws, verdict, sites, budget) -> dict`: the JSON the proposer receives.
- **Clustering:** entities within 2·h of each other, sharing a mesher failure, form one site. Kernel soft findings attach only when inside a site's box.

- [ ] **Step 1:** Write failing tests.
  - `test_soft_findings_outside_sites_ignored`.
  - `test_site_merges_same_failure_across_meshers`.
  - Native: the ABC overlapping-facets file yields at least one `proximity` or `self_overlap` site naming real face IDs.
  - `test_observation_is_json_serialisable_and_under_60kb`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. The k-hop graph is k = 2: surface type, area, min width, angular span, continuity, edge tolerance vs gap, dihedral angles. `render.view(site)` is a kernel fn producing a 512 px PNG.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): failure sites and observations`.

### Task 18: Search driver and trace

**Files:** Create `search.py`, `trace.py`. Test `tests/unit/cad/repair_v4/test_search.py`

**Interfaces:**
- Consumes: Workspace, `REGISTRY`, `run_stage`, `cheap_verdict`, `verdict`, `score`, `find_sites`.
- Produces:
  - `Candidate(op: str, targets: list[StableId], params: dict, source: Literal["det","ai"], rationale: str = "")`.
  - `Generator` protocol: `propose(state: SearchState, k: int) -> list[Candidate]`.
  - `DetGenerator()`: per site kind, a fixed parameter sweep.
  - `Budget(wall_s: float, ops: int, model_calls: int, model_usd: float)`.
  - `search(pool, start: Workspace, original: str, generators: list[Generator], budget: Budget, *, workers: int, k_confirm: int = 3, patience: int = 3) -> SearchResult(best: Workspace, best_verdict: Verdict, clean: bool, trace: Trace)`.
  - `deliverable(result, input_verdict) -> Workspace | None` implements the delivery rule from Global Constraints.

- [ ] **Step 1:** Write failing tests, with a fake pool and a fake verdict.
  - `test_stops_when_clean`.
  - `test_respects_wall_and_op_budget`.
  - `test_confirms_top_k_with_full_panel`.
  - `test_patience_stops_flat_search`.
  - `test_invalid_candidate_returns_error_to_source_not_crash` (Review Focus 5).
  - `test_deliverable_passthrough_when_fewer_panel_passes`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement best-first search with a heap keyed by `score`. Expand the best state; gather candidates from all generators; validate them with `validate_call`; evaluate the valid ones in parallel on forks with `cheap_verdict`; push results; confirm the best `k_confirm` new states with the full `verdict`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): best-first search driver`.

### Task 19: AI proposer

**Files:** Create `proposer.py`. Test `tests/unit/cad/repair_v4/test_proposer.py`

**Interfaces:**
- Consumes: `observation`, `REGISTRY` schemas, `contracts/model_inference.py` (new call kind `repair_v4_propose`, with images and tools; `job_id` passed through).
- Produces:
  - `AIProposer(route: str, *, max_turns: int, usd_ceiling: float)` implements `Generator`.
  - One conversation per file.
  - Tools exposed to the model: `propose(candidates: list[{op|run_stage, targets, params, rationale}])` and `declare_unfixable(site_id, reason)`.
  - After each search round, the proposer is sent the Diffs and verdict deltas of its own candidates.

- [ ] **Step 1:** Write failing tests with a scripted fake model.
  - `test_schema_error_returned_as_tool_result` (Review Focus 5): a removed target is sent back with an error message and the next turn is allowed.
  - `test_usd_ceiling_stops_calls`.
  - `test_conversation_persists_across_rounds`.
  - `test_images_attached_when_site_has_view`.
  - `test_unfixable_site_excluded_from_det_sweeps`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Route defaults to the strongest **OpenAI** model configured for the repair route (owner decision, 2026-11-23: OpenAI on the existing `openai-api-key` route, not Claude). An Anthropic Messages adapter was built as Task 19a and is **deferred — parked unmerged on `sdd/task-19a-anthropic`** should the owner revisit the vendor. The system prompt states:
  - the clean definition;
  - the fidelity bound;
  - that the AI owns overlap intent, hole vs missing face, fix choice under budget, ordering and stopping;
  - that malformed calls cost nothing but a turn.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair_v4): AI proposer`.

---

## Integration and evaluation

### Task 20: Engine flag and dispatch

**Files:**
- Modify:
  - `settings/cad_repair.py` (add `CAD_REPAIR_ENGINE`, values `v3|search`, default `v3`, strict parse)
  - `settings/inventory.py` (roster entry beside `CAD_REPAIR_MESHABILITY_PASS`, line ~412)
  - `docs/reference/configuration.md`
  - the publication manifest (regenerate with the command the contracts test prints)
  - `cad/repair/conservative.py:1027` (`repair_step_file` dispatches to `repair_v4.repair_v4` when the engine is `search`)
- Create: `repair_v4/__init__.py` `repair_v4(source: str, dest: str, *, budget: Budget, deadline_s: float) -> V4Result` mapping to `RepairReport`.
- Test: `tests/unit/cad/repair_v4/test_engine_dispatch.py`, plus the existing settings-roster and contracts tests.

- [ ] **Step 1:** Write failing tests: `test_default_engine_is_v3`; `test_search_engine_dispatches`; `test_unknown_value_rejected`; `test_prod_posture_does_not_set_engine` (parse `.github/workflows/deploy.yml` prod block).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. `V4Result` carries the trace path, the verdict summary and the delivered/passthrough flag.
- [ ] **Step 4:** Run `pytest tests/unit -q -k "settings or contracts or repair_v4"`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(repair): CAD_REPAIR_ENGINE selects the v4 search engine (default v3)`.

### Task 21: Eval arms, waves, report

**Files:**
- Create: `devtools/validation/v4_eval/evalspec.py`, `report.py`, `RUNBOOK.md`
- Test: `tests/unit/dev/test_v4_evalspec.py`

**Interfaces:**
- Produces:
  - Arms: `input_only`, `staged`, `search_det`, `search_ai`, `v3_meshability_pass_ai`.
  - Shared `Budget(wall_s=600, ops=60)`. `search_ai` adds `model_usd=1.00`.
  - Report metrics per split:
    - clean gain;
    - regressions (both kinds);
    - per-layer pass counts;
    - AI marginal = `search_ai − search_det`;
    - deterministic marginal = `staged − input_only`;
    - cost and wall per file;
    - cheap/full agreement.

- [ ] **Step 1:** Write failing tests: `test_arms_share_budget_except_model`; `test_report_flags_regression_when_panel_passes_drop`; `test_heldout_report_requires_frozen_commit`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement on top of Task 8's `run_arm`/`collect`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit. Then, in order:
  - run `input_only` and `staged` on dev;
  - after Tasks 17–19, run `search_det` and `search_ai` on dev;
  - tune on dev only;
  - freeze a commit and run all arms once on held-out and clean;
  - publish the report.

---

## Out of scope for this plan (spec §5.10, later phases)
- Recipe memory and learned per-op predictors: they need data from the dev waves.
- The LLM package reviewer as an extra gate: v4 delivers on the verdict; adding the reviewer back is a one-flag follow-up, measured separately.
- Production routing (triage) and the delivery holes.

## Schedule (parallel lanes)

| Day | Tasks |
|---|---|
| 1 | 1, 2, 6, 7, 9, 10. Task 8 is committed and the re-probe launched by end of day. |
| 2 | 3, 4, 5 (finishing L0); 11–15 in parallel (op lanes); 16 once 11 lands. First `staged` wave on dev as soon as the dev split exists. |
| 3 | 17, 18, 19, 20; first `search_det` / `search_ai` waves |
| 4–5 | Dev tuning waves; held-out freeze and report |

## Task 22 (added 2026-11-22 by the controller) — wire `_run_search`, the engine seam

**Why it exists.** Three tasks each built their side of the engine seam and none owns the join, so
**neither search arm is reachable from the engine today.** `repair_v4/__init__.py:51-60` is still a
stub raising `SearchUnavailable`; the path is `repair_step_file` → `_repair_via_v4` → `repair_v4` →
`_run_search`. Task 18b added `search.arm_generators(arm, **ai_kwargs)` (`search_det` → the
deterministic sweep with no proposer, `search_ai` → `ai_generators`, anything else a `ValueError`)
and correctly stopped there: wiring the stub needs a worker pool, a judge, delivery gating and a
real model route, none of which belong to a wiring task. Task 21's arm table also has to call it.

This was a gap in my decomposition, not in any task. Nobody was asked to own the seam, so each lane
built to its own edge and stopped — and the per-task reviews could not catch it, because every task
was complete against its own brief.

**Scope.**

1. Implement `_run_search` for real: construct the pool, build the judge, call `arm_generators` for
   the named arm, and honour delivery gating.
2. Have Task 21's arm table dispatch through it, so `search_det` and `search_ai` are selectable as
   arms rather than only as library calls.
3. `CAD_REPAIR_ENGINE=search` must actually select it. **Note the separate, still-open gap:
   `CAD_REPAIR_ENGINE` is not plumbed through `create-api-service.sh`/`startup.sh`, so a personal
   dev slug cannot select `search` yet** — decide whether that belongs here or in its own change.
4. A test that proves the arm is reachable end to end, not just that the mapping function returns
   the right generators.

**Binding.** `CheckerUnavailable` stays infrastructure, never a verdict. The $1.00 per-file ceiling
holds through `ledger.admit`'s per-call reservation; `Budget.model_usd` is advisory. Production
posture is untouched: `CAD_REPAIR_ENGINE` defaults to v3 and prod never sets it.

**Blocked on** the panel merging (Task 3), since the real `cheap_verdict` raises `ImportError`
without it.

## Task 24 (added 2026-11-22 by the controller) — the eval harness side

**Why it exists.** Task 21 built the arms, the waves and the report; it deliberately deferred the
harness that produces the rows the report consumes, because the search arms had not landed. So
nothing yet proves a real run emits rows the report can read, and two concrete refusals are waiting
on it.

**Scope.**

1. **`run_arm --mode harness`** writing the arm rows, with the per-row fields Task 21's `ArmRow`
   requires — including Task 18b's `reason`, `verdict_candidates`, `inconclusive_candidates` and
   `infrastructure_failed`. Those must be a real **import** from the search driver, not a mirrored
   copy: two definitions of "was this file measured" that drift apart put the report and the driver
   in disagreement, which is the precise failure those fields exist to prevent.
2. **The launcher must call `plan_wave`**, so a wave's cap and its once/three-times slot are claimed
   rather than assumed.
3. **Copy `plan.provenance()` into the shard provenance.** `run_arm` does not today, so a held-out
   report *refuses* — correctly, since the report cannot otherwise tell that the held-out slot was
   claimed. Fail-closed, but it must be wired before the held-out wave.

**The check that closes it** (Task 21's reviewer named it, and it is the one worth running first):
a small real dev wave with a forced `CheckerUnavailable` — no gmsh — asserting that the arm row reads
`inconclusive: true`, that it is excluded from the clean rate, and that `input_only`'s `in_clean`
matches the split's own verdict. That exercises the infrastructure-is-not-a-verdict rule end to end,
which no unit test can.

**Blocked on** Task 22 (`_run_search` wired, so the search arms are reachable) and Task 3's panel.

**Open owner item carried here:** `clean_control`'s wave type is assumed "normal" because the plan
gives it no cap of its own. Task 21 pinned it with a test and flagged it.

### Task 24 carry-ins from Task 21's review (they can only bite once the harness exists)

Task 21 merged approved, with six soft items. None can bite before Task 24 wires the harness,
because a held-out report cannot be produced at all until `plan.provenance()` reaches the shard
provenance. They land with Task 24:

1. **`primary_artifact` must require `slot_verified`.** Today a self-declared `slot` string earns the
   "[PRIMARY ARTIFACT, frozen commit verified]" banner even with no marker store consulted (the slot
   line does say "marker store NOT consulted"). The held-out split is spent in one use, so the
   banner on an unverified slot is the one label that must not be available cheaply.
2. **Exclude NOT MEASURED arms from the indistinguishable warning.** It can print "X and Y are
   indistinguishable on all N files they share" for an arm whose numbers are withheld, which leaks
   that arm's outcomes on those files.
3. **Per-kind cells on a MEASURED arm** still print `clean/conclusive` beside "withheld" when that
   kind is over 10% inconclusive - the original withholding defect in a different cell, untested.
4. **Assert `clean_gain` and paired `clean_in`/`clean_gain` numerically on a clean split.** The dev
   fixtures all have `in_clean=False`, so `clean_gain == clean_out` and the new paired gain code is
   entirely unasserted: `clean_out + clean_in`, or a dropped `cin`, would pass today.
5. **Add one row with `verdict_candidates=0`, `inconclusive_candidates>0`, `out_clean=True`**, so the
   `not out_clean` clause is exercised; dropping it currently survives.
6. **Replace the mirrored 18b fields with the shared definition** (see Task 24's scope), and check two
   copied details: the third clause uses `out_clean` as a stand-in for "start not clean", equivalent
   only while `verdict_candidates == 0` implies the output is the start; and `SEARCH_ARMS` is a
   second hard-coded list of search arms.

Also noted, not blocking: paired rates are conditioned on files no measured arm dropped, so absolute
rates are flattered relative to all files, and with four measured arms each dropping ~10% on
different files the paired n can fall to ~60% of the split. There is no warning when paired n is much
smaller than the file count, and the pairwise marginals use their own pairwise intersection rather
than the table's paired set - so subtracting two paired-column rates does not reproduce the printed
marginal. Worth a minimum-paired-n warning when the harness makes real numbers available.

## Amendments settled 2026-11-23 (builder calls under design 8; the owner may still override)

1. **Patience scales with the work:** `patience = max(3, open_sites + 2)`, still bounded by the wall
   and op budgets, and **a round that admits a new state counts as progress even if the score ties**.
   Task 42 found `fill_loop` does not improve the score so it burned a round, and a part with three
   or more holes stopped on patience before `make_volume`. A 3-hole part must now reach it (task 46).
2. **Artifact Registry: approved for `hexera-dev` ONLY, never production.** The mesh and validation
   images are pushed there, and the gates and the T2 smoke move onto x86 spot VMs labelled
   `dev-crsv4` with a 3 h self-delete backstop. Both images are already `linux/amd64`, so they run
   natively there with no rebuild. The reason is measured, not aesthetic: gates take 15+ minutes
   under amd64 emulation with six lanes on one Docker daemon, and Task 44 hit
   `kernel worker did not start within 30s` under that contention - a flake in the harness we
   measure with.
3. **`MIN_PAIRED_FRACTION = 0.80`** is the accepted default (Task 21's provisional figure). It must
   be **reported** in the smoke and held-out reports alongside the **actual** paired fraction, so a
   reader sees the threshold and the number it is judging.
4. **`clean_control`'s wave cap** stands as the lanes pinned it (the normal cap, since the plan gives
   it none of its own and it calls no model). Reported the same way.

Both 3 and 4 were pinned with a test rather than guessed, which is why accepting them costs nothing.

## Amendments settled 2026-11-23, 19:15 UTC (from Task 43's measurements)

1. **The cheap-tier-first re-probe is DROPPED. The re-probe is a full verdict on every file.** The
   cheap tier passed 14 files and **6 of those failed the full panel**, so sampling 10% of cheap
   passes would have mislabelled a large share of the corpus. It is also cheaper this way, because
   the cheap pass was an extra ~15.5 s per file on top of a full verdict that mostly duplicated it:
   recomputed from the measured means, **full-tier-only is 249 vCPU-hours (31 c2d-8 VM-hours)
   against 296 (37) for cheap+full** - about 5% of the 600 VM-hour cap. Two caveats travel with
   that number: the mean is from n=73 and the tail dominates it (median 21.9 s vs mean 82.4 s, ~7%
   of files hitting a 300 s or 900 s wall), so a tail 50% worse gives 47 VM-hours and twice as bad
   gives 62; and the 1-vCPU-per-file assumption is unverified, since most of a file's time is layers
   1 and 3 whose threading has not been measured. The conclusion is robust to both.
   The cheap tier survives **only for search ranking**, with its agreement rate reported.
2. **The clean control comes from the full re-probe's measured-clean set, never from a source tag.**
   `abc_pass` is half-unclean - 10 of 20 fail the panel - so it is not a known-clean source. Nothing
   in the splits path may infer "clean" from `source`.
3. **Stratify on `(number of panel members failing, layer)` plus a coarse failure kind, not on a
   single member's family string.** The panel disagrees with itself on **51 of 73 files**, so "the
   family of the first member to fail" labels an unstable quantity; the count of failing members is
   a real property of the file and is the axis that matters for a benchmark premised on
   mesher-agnostic cleanliness. Routed into Task 48, replacing its original family-recognition item.
4. The T2 smoke proceeds once Tasks 47 and 48 land.

Recorded because the first three change what the benchmark measures, not merely how fast it runs.

## Amendment settled 2026-11-23, 21:37 UTC - WAVE_SHARE and the wave-level 429 guard

Lane B3's header probe found that gpt-6-astra's limit on our key, 15,000 RPM /
40M TPM, is **shared with gpt-5.6-sol and gpt-5.6-terra - i.e. with production**.
No project-scoped headers were present, so a paid wave competes with live traffic.
Lane B3 set WAVE_SHARE = 0.5 and said plainly it was a guess at production's
headroom.

**Settled: WAVE_SHARE = 0.05** - about 750 RPM / 2M TPM. The reasoning is that
it protects production and the owner can raise it: a 96-shard wave needs at most
~200 RPM (a shard runs one file at a time, so at most 96 calls are in flight),
so 0.05 fits with ~3.7x headroom while leaving 95% of the limit to production.
Conservative wins because the downside is asymmetric - a throttled wave is slow,
a starved production is an incident.

**Also required: a WAVE-LEVEL 429 guard.** If 429s exceed a small threshold in
any 5-minute window the wave backs off GLOBALLY, not per shard, and the report
records it. Per-shard cool-down is not enough: 96 shards each backing off
independently still presents 96 shards' worth of pressure to a limit that is
already shared.

### The four mechanisms that would have distorted the AI's marginal value

To be carried into the final report's method section, because each one credited
or debited the model for work it did not do, and all four were found only by
measuring:

1. **Undescribed ops** - OP_SUMMARIES gave no description for 13 of 19 ops, so
   the model chose among ops it had not been told about (Lane B).
2. **A wrong definition of clean** - the prompt's prose was wrong in three
   places until it was generated from the checker's own constants (Lane B2).
3. **The dedupe discarding the repair** - the orientation-blind fingerprint
   could drop orient_and_close's own output as a duplicate (Task 45).
4. **Unreached-model rows charged to the AI** - a search_ai file whose model was
   never reached wrote a CONCLUSIVE row, so its det-only outcome was recorded as
   an AI result (Lane B3).

Standing separately: across three live runs and one fake-model run, the winning
candidates have always been DETERMINISTIC, with the AI's identical proposals
arriving as duplicates. The ~$0.07 AI-only ablation is the outstanding
experiment.

## Amendments settled 2026-11-23, 22:10 UTC

5. **Excluded files: raise the walls, report the remainder as a stratum.** Lane
   D3 measured wall-excluded files at ~8.7% pool-weighted against `collect`'s
   hard 2% limit, so the re-probe would have ended at exit 2 with nothing. The
   per-file and `check_valid` walls are raised to values chosen from the
   measured timeout distribution (the chosen percentile and the residual
   exclusion fraction are stated in the report, not rounded), and whatever
   remains excluded is carried as an explicit reported stratum - counted and
   named, never a failure, never in clean gain or regressions. `--allow-excluded`
   is never passed blindly; if the raised walls still breach the limit the wave
   refuses and says why. Booked as Task 55.

6. **The op budget is charged per applied op.** Lane C3 found a single candidate
   can apply a chain of ops for one budget unit (`shells` = sew + make_volume),
   which hands extra geometry work to whichever arm happens to route through a
   multi-op stage. `run_stage` now charges the real applied-op count for every
   arm. Booked as Task 53.

7. **The deterministic baseline must reach every op the AI can reach.**
   `tighten_tolerances`, `fix_poles` and `to_canonical` ran only in the
   pre-search staged healer, so during the search only the AI could re-run them.
   They are added as search-time deterministic candidates, in the same class as
   Lane C3's fix, and the coverage assertion is extended so the next op added
   cannot repeat the bug. Booked as Task 54.

**The AI marginal is measured only on a commit that contains Tasks 53 and 54.**
Any earlier number overstates the AI, because it credits access to ops rather
than judgement. The same applies to the re-probe and D4: the determinism fix
must land first, or the re-probe's labels inherit the checker's flipping - two
sources of run-to-run nondeterminism are now measured (a near-threshold min SICN
of 0.0042 against the 0.01 floor flipping `gmsh_alt`, and a corrupted file
flipping mesher-crash against mesher-failure), and splits built from one run of
that checker will disagree with the next run on those files.

## Amendment settled 2026-11-24, 02:47 UTC - THE GOAL CHANGED: clean is valid + panel

Owner decision. It **supersedes design section 7 items 1-3 and the whole
gating role of the faithful layer**. It is **not a rearchitecture**: the v4
design stays exactly as built - the staged healer, failure sites, best-first
search, the AI proposer and the eval are unchanged. Only what the engine is
*aiming at* changed.

1. **CLEAN = layer 1 VALID + layer 2 PANEL.** All four meshers, min SICN
   `>= 0.01`, no hints. **Faithfulness no longer gates clean.** Every row still
   REPORTS deviation, volume and area change, mass, bbox and body-count change,
   but none of them can fail a clean verdict or refuse an edit.

2. **The geometry/fidelity caps are REMOVED, not loosened:** the deviation bound
   `min(0.1 mm, 0.1% diag)` and the per-op deviation budget; the area/volume
   caps as applied in v4; the 5%-of-diagonal `fill_loop` limit; the
   overlap-must-fit-the-bound rule on `cut_by`; the no-fuse rule; the
   `remove_small` width bound; and op parameter bounds tied to the deviation
   budget. **Face removal is allowed** (defeaturing). The score tuple becomes
   `(valid, panel passes, min SICN)`, with deviation a **tie-breaker only**.

   **KEPT, and not negotiable:** the spend caps ($/file, wave caps, VM budget),
   the worker kill/timeout safety, the sealed held-out set, no merge to main, no
   deploy, no production flags.

3. **Raymond's approach is adopted** (`origin/raymond/cad-simplification`,
   `docs/superpowers/specs/2026-11-19-cad-simplification-design.md`,
   `docs/simplification/DEVLOG.md`). What we need is **ported and attributed**
   into `repair_v4`; his branch is **not merged**. New ops: `defeature(faces)`
   via `BRepAlgoAPI_Defeaturing`, with his feature recogniser as a site and
   candidate source (holes and blends, **never slot ends** - OCCT returns a
   180-degree concave round unchanged, as his DEVLOG records); `fuse(bodies)`;
   `cut_by(a, b)` at **any** overlap depth; and `trim_unbounded`, a Common with
   an enlarged part bbox that bounds infinite surfaces to the part's real
   extent. The search may now use aggressive edits to reach clean.

4. **Gaming is reported, not capped.** The report flags any CLEAN output whose
   volume changed more than 25% or whose bbox shrank more than 10%, and lists
   them per arm, so a "clean" cube cannot hide inside the headline. The owner
   reviews that list; the files still count as clean.

5. **Sequencing.** Let snapshot v1r finish under the OLD definition, then
   **re-score v1 and v1r from the recorded layers, with no rerun** - that is the
   new baseline, free. The four jobs run in parallel, one per lane: the verdict
   and report change (A10), the cap removal (B12), `defeature` (D14), and the
   body and unbounded ops (C7). Then snapshot v2 on the 150-file dev split under
   the new definition, cap $50; v2 launches as soon as A10 and B12 are in, and
   D14 and C7 make it better. The verdict cache version is bumped, so every
   cached verdict misses.

**Why.** Snapshot v1 measured an AI marginal of **0** on every file, and the
diagnosis was not that the model lacked judgement: 57% of its proposals died
inside the ops, its cheap-tier passes were vacuous because the tier could not
see the member that fails, and it was never asked a second time. Those are
fixed. What remains is that the op library cannot express the edit many of these
defects need, and the fidelity caps refused the edits that could. The owner's
judgement is that a mesher-clean part the customer can run is worth more than a
part held bit-faithful and unmeshable - so the caps go, the aggressive ops land,
and faithfulness becomes something we REPORT and the owner reads, not something
that silently refuses a repair.
