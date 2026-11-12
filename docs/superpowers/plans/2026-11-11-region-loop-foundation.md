# Region Loop Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CAD repair operate on regions with locally-proved, individually-reversible target packages, driven deterministically, so the model and the new primitives can be dropped in afterwards without touching the loop.

**Architecture:** Defects are clustered into conservative regions. Each region receives a target package - a locus, an intent, an ordered plan of catalogue operations, a predicate asserted before execution, and its own budget. A package executes, is proved by a region-scoped re-inspection plus cheap global invariants, and commits or reverts as one unit. This plan supplies regions, scoped scanning, the package and predicate types, per-region budgets, package-level reversal, and a deterministic package author. It adds no model call and no new geometry primitive.

**Tech Stack:** Python 3.11, OpenCASCADE via OCP, pytest. Pure judgement is unit-tested in `tests/unit/cad/`; anything touching the kernel is tested in `tests/native/`.

**Spec:** `docs/superpowers/specs/2026-11-11-scoped-llm-cad-repair-design.md`

## Global Constraints

- Posture must fail closed: an unrecognised `CAD_REPAIR_*` value reads as off, per `settings/cad_repair.py:_planner_posture`.
- A package is a planner directive, never a cap waiver. `sanity_refusal` and `cumulative_refusal` judge every package exactly as they judge today's candidates (`cad/repair/directives.py` header).
- Inviolable invariants, which no package may request an exemption from: no inverted or degenerate elements, no lost solids, manifold output, valid shape.
- `complete_scan` false dominates every other verdict. A short list from a stopped walk supports no claim of absence (`cad/repair/loop.py:_terminal`).
- Measurements are millimetres. Entity fingerprints are content-derived and stable across runs.
- Host gates must pass before any deploy: `make lint`, `make typecheck`, `make test-fast`, `make check-fast`.
- Unit tests run via `make test-fast`; a single test needs the project venv (`make setup` if `pytest` is missing). Native tests run via `bash tests/native/run_tier.sh all`.

## Review Focus

Five conditions the spec implies that no task's happy path exercises. Each has a test assigned to the task that owns the code.

- A sound part with zero defects must author zero packages and run exactly one scan. (Task 7)
- A part whose defects all fall in one region must still work, with no parallelism to rely on. (Task 3)
- A vacuous predicate - one that asserts only what is always true, such as validity - must be refused as non-specific, or the author learns to promise nothing. (Task 5)
- Scan budget exhaustion inside a region must leave `complete_scan` false and dominate the region's verdict, never commit on a partial list. (Task 6)
- A region where every defect is discharged by reclassification rather than geometry must be reported with its reclassification count, so "clean by relabelling" is visible. (Task 6)

---

### Task 1: Repair Run Timing

The report records no elapsed time anywhere today, so nothing later in this plan can be shown to have made anything faster.

**Files:**
- Modify: `src/meshpipeline/cad/repair/session.py` (`OpVerdict`, `try_op`)
- Modify: `src/meshpipeline/cad/repair/loop.py` (`RepairRun`, `run`)
- Modify: `src/meshpipeline/cad/repair/conservative.py:827` (the `measurements` dict)
- Test: `tests/unit/cad/test_repair_timing.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `OpVerdict.apply_s: float` and `OpVerdict.scan_s: float`, both defaulting to `0.0` and included in `to_dict()`; `OpVerdict.measure_s: float`; `RepairRun.total_s: float` and `RepairRun.scan_count: int`, both in
  `to_dict()`; report key `measurements["timings"] = {"total_s": float, "scan_count": int, "measure_s": float}`.
  `measure_s` is timed separately because `caps.measure` runs a whole-shape `BRepCheck_Analyzer` and is
  called twice per operation (`session.py:231`, `:242`), so it is a second cost centre alongside the scan
  and a scoped proof has to scope it too.

- [ ] **Step 1: Write the failing test**

```python
def test_verdict_records_apply_and_scan_seconds():
    # loop.run accepts clock=; drive a fake clock so timings are exact, not flaky.
    verdicts, run = _run_with_clock([0.0, 0.5, 1.5, 2.0])
    assert verdicts[0].apply_s == 0.5
    assert verdicts[0].scan_s == 1.0

def test_run_reports_total_and_scan_count():
    _, run = _run_with_clock([0.0, 0.5, 1.5, 2.0])
    assert run.total_s == 2.0
    assert run.scan_count == 2  # the first scan plus one re-inspection
    assert run.to_dict()["total_s"] == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/cad/test_repair_timing.py -v`
Expected: FAIL with `AttributeError: 'OpVerdict' object has no attribute 'apply_s'`

- [ ] **Step 3: Implement the timing fields**

Thread the loop's injected `clock` into `RepairSession` so `try_op` can time step 2 (apply) and step 5 (re-inspect) separately; the session must not call `time.monotonic` directly, or the fake clock cannot drive it. Increment `scan_count` in `RepairSession._rescan` and in the first `defects()` call.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/cad/test_repair_timing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/session.py src/meshpipeline/cad/repair/loop.py src/meshpipeline/cad/repair/conservative.py tests/unit/cad/test_repair_timing.py
git commit -m "feat(repair): record elapsed time per operation, scan and run"
```

---

### Task 2: Make The Unchecked Volume Visible And Bound The Closed Bodies

`cumulative_refusal` skips the volume comparison whenever `original.solids == 0` - the exact case
fills convert into solids. Measured on the knee: volume moved -6454.25 mm3 (-12%) with
`declared_volume_mm3: 0.0` and nothing looked at it.

It cannot be closed by comparing before against after. `caps.py:189` is explicit that an open
shell's volume integral "is a number, not a volume - it reports 799.9999999999998 mm3 for the
5-face open box of the open-shell fixture", so the "before" side of any such delta is meaningless.
This task therefore bounds what is boundable and makes the unchecked case explicit instead of
silent.

**Files:**
- Modify: `src/meshpipeline/cad/repair/caps.py` (`cumulative_refusal`)
- Modify: `src/meshpipeline/cad/repair/conservative.py` (add to `measurements`)
- Test: `tests/unit/cad/test_repair_caps.py` (extend)

**Interfaces:**
- Consumes: `Measure` as it is.
- Produces: `volume_accountability(original: Measure, current: Measure) -> dict` returning
  `{"compared": bool, "reason": str}`; report key `measurements["volume_accountability"]`.
  `cumulative_refusal` gains a plausibility refusal for the closing case and is otherwise unchanged.

The plausibility bound is geometric and exact: any shape whose bounding-box diagonal is `d` has
volume at most `d**3 / (3 * sqrt(3))`, the cube case. A closed body must also have positive volume.
That catches an integral that has gone wrong by orders of magnitude without inventing a baseline the
geometry cannot support.

- [ ] **Step 1: Write the failing test**

```python
def test_a_closed_body_with_impossible_volume_is_refused():
    original = _changed(solids=0, diagonal_mm=260.475, volume_mm3=53367.667)
    current = _changed(solids=5, diagonal_mm=260.475, volume_mm3=9.9e9)
    assert "volume" in cumulative_refusal(original, current, declared_area_mm2=0.0,
                                          declared_volume_mm3=0.0)

def test_a_closed_body_with_non_positive_volume_is_refused():
    original = _changed(solids=0, diagonal_mm=260.475, volume_mm3=53367.667)
    current = _changed(solids=5, diagonal_mm=260.475, volume_mm3=0.0)
    assert "volume" in cumulative_refusal(original, current, declared_area_mm2=0.0,
                                          declared_volume_mm3=0.0)

def test_the_knee_shaped_closing_run_still_passes():
    # the real measured numbers: a legitimate shells-to-solids repair must not be refused
    original = _changed(solids=0, diagonal_mm=260.47491719601175, volume_mm3=53367.667278043045)
    current = _changed(solids=5, diagonal_mm=260.47491719601175, volume_mm3=46913.42038292233)
    assert cumulative_refusal(original, current, declared_area_mm2=0.0,
                              declared_volume_mm3=0.0) == ""

def test_accountability_says_volume_was_not_compared_and_why():
    original = _changed(solids=0, volume_mm3=53367.667)
    current = _changed(solids=5, volume_mm3=46913.420)
    out = volume_accountability(original, current)
    assert out["compared"] is False
    assert "open shell" in out["reason"]

def test_accountability_says_volume_was_compared_for_an_ordinary_run():
    assert volume_accountability(_SOUND, _changed(volume_mm3=8100.0))["compared"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/cad/test_repair_caps.py -k "volume or accountability" -v`
Expected: FAIL - `volume_accountability` is undefined, and the impossible-volume case returns `""`.

- [ ] **Step 3: Implement the plausibility refusal and `volume_accountability`**

Add the plausibility check inside the existing `original.solids == 0 or current.solids == 0 or
current.solids > original.solids` branch, before its `return ""`, so every case that currently
skips the comparison is still skipped but no longer unexamined. Keep the existing `logger.info`
line and the comment's reasoning; extend the comment to say what is now bounded.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/cad/test_repair_caps.py -v`
Expected: PASS, with every pre-existing test in the file still passing.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/caps.py src/meshpipeline/cad/repair/conservative.py tests/unit/cad/test_repair_caps.py
git commit -m "fix(repair): bound closed-body volume and report when volume went uncompared"
```

---

### Task 3: Region Decomposition

**Files:**
- Create: `src/meshpipeline/cad/repair/regions.py`
- Test: `tests/unit/cad/test_repair_regions.py`

**Interfaces:**
- Consumes: the defect objects `localize_scan` returns - each carries `fingerprint`, `entity_type`, `entity_index`, `severity`, and `location` with `bbox_min`, `bbox_max`, `centroid`.
- Produces:
  - `@dataclass(frozen=True, slots=True) Region` with `index: int`, `fingerprints: frozenset[str]`, `bbox_min: tuple[float, float, float]`, `bbox_max: tuple[float, float, float]`.
  - `decompose(defects, *, diagonal_mm: float, merge_fraction: float = 0.01) -> tuple[Region, ...]`

Clustering must be **conservative**: over-merge rather than under-merge, because a self-intersection tangle is reported once per member face and splitting one tangle across regions would let two packages fight over the same geometry. Two defects join the same region when their bounding boxes, each grown by `merge_fraction * diagonal_mm`, intersect. Regions are returned in a deterministic order (ascending `bbox_min`) so two runs over the same bytes produce the same regions.

- [ ] **Step 1: Write the failing test**

```python
def test_distant_defects_form_separate_regions():
    regions = decompose([_defect("a", at=(0, 0, 0)), _defect("b", at=(100, 0, 0))],
                        diagonal_mm=200.0)
    assert len(regions) == 2

def test_near_defects_merge_into_one_region():
    regions = decompose([_defect("a", at=(0, 0, 0)), _defect("b", at=(0.5, 0, 0))],
                        diagonal_mm=200.0)
    assert len(regions) == 1
    assert regions[0].fingerprints == frozenset({"a", "b"})

def test_merging_is_transitive_so_a_chain_is_one_region():
    # a--b and b--c close, a--c far: all three must land together
    regions = decompose([_defect("a", at=(0, 0, 0)), _defect("b", at=(1.5, 0, 0)),
                         _defect("c", at=(3.0, 0, 0))], diagonal_mm=200.0)
    assert len(regions) == 1

def test_order_is_deterministic():
    defects = [_defect("b", at=(100, 0, 0)), _defect("a", at=(0, 0, 0))]
    assert [r.index for r in decompose(defects, diagonal_mm=200.0)] == [0, 1]
    assert decompose(defects, diagonal_mm=200.0)[0].fingerprints == frozenset({"a"})

def test_one_region_is_a_valid_decomposition():
    # Review Focus: a part whose defects all fall in one region must work.
    regions = decompose([_defect("a", at=(0, 0, 0)), _defect("b", at=(0.2, 0, 0)),
                         _defect("c", at=(0.4, 0, 0))], diagonal_mm=200.0)
    assert len(regions) == 1 and len(regions[0].fingerprints) == 3

def test_no_defects_is_no_regions():
    assert decompose([], diagonal_mm=200.0) == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/cad/test_repair_regions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'meshpipeline.cad.repair.regions'`

- [ ] **Step 3: Implement `decompose` in `src/meshpipeline/cad/repair/regions.py`**

Union-find over grown bounding boxes gives transitivity for free and is the obvious fit; a single pass of pairwise merging does not, and the chain test will catch that.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/cad/test_repair_regions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/regions.py tests/unit/cad/test_repair_regions.py
git commit -m "feat(repair): cluster located defects into conservative regions"
```

---

### Task 4: Region-Scoped Re-Inspection

The primitive that makes iteration affordable. `localize_scan` always walks the whole shape; after an edit inside one region, every other region is re-proved for nothing.

**Files:**
- Modify: `src/meshpipeline/cad/repair/localize.py:849` (`localize_scan`)
- Test: `tests/native/test_native_repair_scoped_scan.py`

**Interfaces:**
- Consumes: `Region` from Task 3.
- Produces: `localize_scan(shape, *, valid=None, face_scan_s=0.0, within=None)`, where `within: Region | None`. `None` preserves today's whole-shape behaviour byte-for-byte. When given, only entities whose bounding box intersects the region's grown box are walked, and the returned completeness flag refers to that region alone.

This is a native test because it needs a real kernel and a real part.

- [ ] **Step 1: Write the failing test**

```python
def test_scoped_scan_finds_the_same_defects_in_its_region_as_a_global_scan():
    shape = _read(FIXTURE)                      # a part with defects in two separated places
    everything, _ = localize_scan(shape)
    regions = decompose(everything, diagonal_mm=_diagonal(shape))
    for region in regions:
        scoped, complete = localize_scan(shape, within=region)
        assert complete
        assert {d.fingerprint for d in scoped} == {
            d.fingerprint for d in everything if d.fingerprint in region.fingerprints}

def test_omitting_within_is_unchanged():
    shape = _read(FIXTURE)
    assert [d.fingerprint for d in localize_scan(shape)[0]] == \
           [d.fingerprint for d in localize_scan(shape, within=None)[0]]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/native/run_tier.sh all` (or the single file inside the mesh image)
Expected: FAIL with `TypeError: localize_scan() got an unexpected keyword argument 'within'`

- [ ] **Step 3: Add the `within` parameter**

Filter the entity walk by bounding-box intersection before any per-entity measurement, so the saving is in work avoided rather than results discarded. The stable sort order (entity type then index) and the completeness contract both stay exactly as documented in the existing docstring.

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/native/run_tier.sh all`
Expected: PASS, and the existing native repair tests still pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/localize.py tests/native/test_native_repair_scoped_scan.py
git commit -m "feat(repair): scope a re-inspection to one region"
```

---

### Task 5: Target Package And Predicate Evaluator

**Files:**
- Create: `src/meshpipeline/cad/repair/package.py`
- Test: `tests/unit/cad/test_repair_package.py`

**Interfaces:**
- Consumes: `Region` (Task 3).
- Produces:
  - `@dataclass(frozen=True, slots=True) PackageBudget` with `max_ops: int`, `timeout_s: float`, `model_calls: int`, `deviation_mm: float`.
  - `@dataclass(frozen=True, slots=True) Predicate` with `cleared: frozenset[str]`, `reclassified: frozenset[str]`, `unchanged: frozenset[str]`, `max_area_delta_mm2: float`, `max_volume_delta_mm3: float`.
  - `@dataclass(frozen=True, slots=True) TargetPackage` with `region: Region`, `intent: str`, `plan: tuple[dict, ...]`, `predicate: Predicate`, `budget: PackageBudget`, `rationale: str = ""`.
  - `specificity(predicate: Predicate) -> float` - 0.0 when the predicate asserts nothing about named entities, rising with the number of entities it commits to.
  - `evaluate(predicate, *, before, after, defects_before, defects_after) -> str` - `""` accepts; otherwise the first way the claim failed, in words.

`PackageBudget.deviation_mm` is **recorded, not enforced** in this plan. Enforcing it would make a
package a cap waiver, which `directives.py:4` says a planner directive is not; whether a package may
genuinely move a cap is an open product-owner question in the spec. The global residual ratios keep
judging every operation.

`evaluate` is the verifier: it compares the claim the author made *before* execution against what was measured. A predicate naming no entity in `cleared`, `reclassified` or `unchanged` scores 0.0 specificity and is refused by the caller, so an author cannot pass by promising only that the shape stays valid.

- [ ] **Step 1: Write the failing test**

```python
def test_predicate_holds_when_named_defects_cleared_and_others_untouched():
    p = Predicate(cleared=frozenset({"edge:a"}), reclassified=frozenset(),
                  unchanged=frozenset({"face:b"}), max_area_delta_mm2=10.0,
                  max_volume_delta_mm3=0.0)
    assert evaluate(p, before=_SOUND, after=_changed(surface_area_mm2=1005.0),
                    defects_before=("edge:a", "face:b"), defects_after=("face:b",)) == ""

def test_predicate_fails_when_a_cleared_defect_remains():
    p = Predicate(cleared=frozenset({"edge:a"}), reclassified=frozenset(),
                  unchanged=frozenset(), max_area_delta_mm2=10.0, max_volume_delta_mm3=0.0)
    assert "edge:a" in evaluate(p, before=_SOUND, after=_SOUND,
                                defects_before=("edge:a",), defects_after=("edge:a",))

def test_predicate_fails_when_an_unchanged_defect_disappeared():
    # collateral damage: the author said it would not touch this, and it did
    p = Predicate(cleared=frozenset(), reclassified=frozenset(),
                  unchanged=frozenset({"face:b"}), max_area_delta_mm2=10.0,
                  max_volume_delta_mm3=0.0)
    assert "face:b" in evaluate(p, before=_SOUND, after=_SOUND,
                                defects_before=("face:b",), defects_after=())

def test_predicate_fails_when_area_moved_past_the_claim():
    p = Predicate(cleared=frozenset({"edge:a"}), reclassified=frozenset(),
                  unchanged=frozenset(), max_area_delta_mm2=1.0, max_volume_delta_mm3=0.0)
    assert "area" in evaluate(p, before=_SOUND, after=_changed(surface_area_mm2=1100.0),
                              defects_before=("edge:a",), defects_after=())

def test_reclassified_defect_may_remain_and_still_hold():
    p = Predicate(cleared=frozenset(), reclassified=frozenset({"face:b"}),
                  unchanged=frozenset(), max_area_delta_mm2=0.0, max_volume_delta_mm3=0.0)
    assert evaluate(p, before=_SOUND, after=_SOUND,
                    defects_before=("face:b",), defects_after=("face:b",)) == ""

def test_vacuous_predicate_scores_zero_specificity():
    # Review Focus: a predicate that promises nothing must be refusable.
    assert specificity(Predicate(frozenset(), frozenset(), frozenset(), 1e9, 1e9)) == 0.0

def test_specificity_rises_with_entities_committed_to():
    one = Predicate(frozenset({"a"}), frozenset(), frozenset(), 1.0, 0.0)
    two = Predicate(frozenset({"a"}), frozenset(), frozenset({"b"}), 1.0, 0.0)
    assert specificity(two) > specificity(one) > 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/cad/test_repair_package.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'meshpipeline.cad.repair.package'`

- [ ] **Step 3: Implement the types, `specificity` and `evaluate`**

`evaluate` returns the first failure in a fixed order - cleared, unchanged, reclassified, area, volume - so the message is deterministic for the same inputs.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/cad/test_repair_package.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/package.py tests/unit/cad/test_repair_package.py
git commit -m "feat(repair): target package, predicate and the evaluator that judges it"
```

---

### Task 6: Package Execution With Region Budget And Reversal

**Files:**
- Modify: `src/meshpipeline/cad/repair/session.py`
- Test: `tests/unit/cad/test_repair_package_session.py`

**Interfaces:**
- Consumes: `TargetPackage`, `Predicate`, `evaluate` (Task 5); `localize_scan(..., within=)` (Task 4).
- Produces: `RepairSession.try_package(package: TargetPackage) -> PackageVerdict`, and `@dataclass(frozen=True, slots=True) PackageVerdict` with `region_index: int`, `status: str` in `{"committed", "reverted", "escalated"}`, `reason: str`, `verdicts: tuple[OpVerdict, ...]`, `reclassified: frozenset[str]`, `scan_s: float`, `apply_s: float`.

One snapshot is taken at the package boundary. Operations inside the package run through the existing `try_op` so every cap, hold and declaration still applies per operation. The package then gets one region-scoped re-inspection, and `evaluate` decides. On any failure the whole package reverts to the boundary snapshot - a partially applied package is never committed, because its predicate was written about the whole plan.

- [ ] **Step 1: Write the failing test**

```python
def test_package_commits_when_its_predicate_holds():
    verdict = _session().try_package(_package(cleared={"edge:a"}))
    assert verdict.status == "committed"

def test_package_reverts_whole_plan_when_predicate_fails():
    session = _session()
    before = session.current()
    verdict = session.try_package(_package(cleared={"edge:never-cleared"}))
    assert verdict.status == "reverted"
    assert session.current() == before          # every operation in the plan undone

def test_package_escalates_when_it_exhausts_its_budget():
    verdict = _session().try_package(_package(cleared={"edge:a"}, max_ops=0))
    assert verdict.status == "escalated"
    assert "budget" in verdict.reason

def test_incomplete_region_scan_never_commits():
    # Review Focus: a partial list supports no claim of absence.
    verdict = _session(scan_complete=False).try_package(_package(cleared={"edge:a"}))
    assert verdict.status != "committed"
    assert "scan" in verdict.reason

def test_reclassified_entities_are_reported_on_the_verdict():
    # Review Focus: clean-by-relabelling must be visible, not silent.
    verdict = _session().try_package(_package(reclassified={"face:b"}))
    assert verdict.reclassified == frozenset({"face:b"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/cad/test_repair_package_session.py -v`
Expected: FAIL with `AttributeError: 'RepairSession' object has no attribute 'try_package'`

- [ ] **Step 3: Implement `try_package`**

Order matters as it does in `try_op`, and for the same reason: refuse before paying for the re-inspection. Check the budget and the predicate's specificity first, then apply the plan through `try_op`, then take one scoped scan, then `evaluate`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/cad/test_repair_package_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/session.py tests/unit/cad/test_repair_package_session.py
git commit -m "feat(repair): execute a target package as one reversible unit"
```

---

### Task 7: Deterministic Region Loop

Proves the whole machinery with no model involved. The rules planner authors one package per region from the existing catalogue, so a regression here is a bug in the loop rather than in a prompt.

**Files:**
- Create: `src/meshpipeline/cad/repair/region_loop.py`
- Modify: `src/meshpipeline/cad/repair/conservative.py` (call the region loop when the posture selects it)
- Test: `tests/unit/cad/test_repair_region_loop.py`, `tests/native/test_native_repair_region_loop.py`

**Interfaces:**
- Consumes: `decompose` (3), `localize_scan(..., within=)` (4), `TargetPackage` (5), `try_package` (6).
- Produces: `run_regions(shape, *, author, tolerance_mm, budget, reserved=frozenset()) -> RegionRun`, and `@dataclass(frozen=True, slots=True) RegionRun` with `shape`, `packages: tuple[PackageVerdict, ...]`, `regions: tuple[Region, ...]`, `escalated: tuple[int, ...]`, `stopped_because: str`, `complete_scan: bool`, `timings: dict`.
- `author` is a callable `(Region, defects) -> TargetPackage | None`, so Plan 2 can substitute the model for the deterministic author without changing this file.

Committed regions ship and an escalated region does not block them, per decision 7. The final verdict comes from one whole-shape scan after all regions are merged.

- [ ] **Step 1: Write the failing test**

```python
def test_a_sound_part_authors_no_packages_and_scans_once():
    # Review Focus: zero defects in, zero work done.
    run = run_regions(_sound_shape(), author=_rules_author, tolerance_mm=0.1,
                      budget=_budget())
    assert run.packages == ()
    assert run.timings["scan_count"] == 1
    assert run.stopped_because == "resolved"

def test_each_region_gets_one_package():
    run = run_regions(_two_region_shape(), author=_rules_author, tolerance_mm=0.1,
                      budget=_budget())
    assert len(run.regions) == 2
    assert len(run.packages) == 2

def test_an_escalated_region_does_not_block_the_others():
    run = run_regions(_two_region_shape(), author=_author_failing_region(1),
                      tolerance_mm=0.1, budget=_budget())
    assert run.escalated == (1,)
    assert [p.status for p in run.packages] == ["committed", "escalated"]

def test_scoped_work_costs_fewer_scans_than_one_scan_per_operation():
    run = run_regions(_two_region_shape(), author=_rules_author, tolerance_mm=0.1,
                      budget=_budget())
    assert run.timings["scan_count"] < 1 + sum(len(p.verdicts) for p in run.packages)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/cad/test_repair_region_loop.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'meshpipeline.cad.repair.region_loop'`

- [ ] **Step 3: Implement `run_regions`**

Regions are independent, so they may be walked in any order; keep them in `decompose` order for a deterministic report.

- [ ] **Step 4: Run both tiers**

Run: `python -m pytest tests/unit/cad/test_repair_region_loop.py -v` then `bash tests/native/run_tier.sh all`
Expected: PASS in both, with the existing `tests/native/test_native_repair_loop.py` unchanged and still passing - the old loop stays reachable for the benchmark baseline, per decision 6.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/region_loop.py src/meshpipeline/cad/repair/conservative.py tests/unit/cad/test_repair_region_loop.py tests/native/test_native_repair_region_loop.py
git commit -m "feat(repair): region loop driving one target package per region"
```

---

### Task 8: Disposition Ledger In The Report

Answers "did repair cause this?" by machine. Doing it by hand on one part took an afternoon.

**Files:**
- Create: `src/meshpipeline/cad/repair/disposition.py`
- Modify: `src/meshpipeline/cad/repair/conservative.py` (add to `measurements`)
- Test: `tests/unit/cad/test_repair_disposition.py`

**Interfaces:**
- Consumes: the first scan's defects and the final scan's defects.
- Produces: `ledger(before, after) -> dict` returning `{"inherent": [fp, ...], "introduced": [fp, ...], "resolved": [fp, ...], "reclassified": [fp, ...]}`, sorted for stable reports; report key `measurements["disposition"]`.

A defect is `inherent` when its fingerprint and its measurement are both unchanged from the upload - identity of the fingerprint alone is not enough, because a same-named entity whose measurement moved was touched.

- [ ] **Step 1: Write the failing test**

```python
def test_unchanged_defect_is_inherent():
    out = ledger(before=[_d("edge:a", length=0.000134865)],
                 after=[_d("edge:a", length=0.000134865)])
    assert out["inherent"] == ["edge:a"] and out["introduced"] == []

def test_new_fingerprint_is_introduced():
    out = ledger(before=[], after=[_d("edge:b", length=0.5)])
    assert out["introduced"] == ["edge:b"]

def test_cleared_defect_is_resolved():
    out = ledger(before=[_d("edge:a", length=0.5)], after=[])
    assert out["resolved"] == ["edge:a"]

def test_same_fingerprint_with_a_moved_measurement_is_not_inherent():
    out = ledger(before=[_d("edge:a", length=0.5)], after=[_d("edge:a", length=0.9)])
    assert out["inherent"] == [] and out["introduced"] == ["edge:a"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/cad/test_repair_disposition.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'meshpipeline.cad.repair.disposition'`

- [ ] **Step 3: Implement `ledger`**

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/cad/test_repair_disposition.py -v`
Expected: PASS

- [ ] **Step 5: Run the full host gates and commit**

```bash
make lint && make typecheck && make test-fast && make check-fast
git add src/meshpipeline/cad/repair/disposition.py src/meshpipeline/cad/repair/conservative.py tests/unit/cad/test_repair_disposition.py
git commit -m "feat(repair): report whether each surviving defect was inherent or introduced"
```

---

### Task 9: Scope The Per-Operation Re-Inspection

Added after Task 7 revealed that the plan's cost thesis does not yet hold. `try_op` re-inspects the
WHOLE shape after every operation, so a package of N operations pays N whole-shape scans inside
`try_op` plus one scoped scan at the package boundary. The scoped scan is additive, and on total
scan count the region loop is currently MORE expensive than the baseline. "Local edit, local proof"
is the justification for the entire project and it is unimplemented until an operation inside a
package also proves itself locally.

The fix is to scope the per-operation re-inspection, NOT to skip it: `try_op`'s progress rule needs
a rescan to know whether the defect it aimed at actually went, and that rule is what stops an
operation being kept for doing nothing.

**Files:**
- Modify: `src/meshpipeline/cad/repair/session.py` (`try_op`, `_rescan`, `try_package`)
- Test: `tests/unit/cad/test_repair_package_session.py` (extend)
- Test: `tests/native/test_native_repair_region_loop.py` (extend)

**Interfaces:**
- Consumes: `Region` (Task 3), `localize_scan(..., within=)` (Task 4), `try_package` (Task 6).
- Produces: `RepairSession.try_op(..., within: Region | None = None)` and
  `RepairSession._rescan(*, face_scan: bool, within=None)`. `try_package` passes its
  `package.region` to every `try_op` it calls. `within=None` preserves today's whole-shape
  behaviour exactly, so `cad/repair/loop.py` is unaffected.

**The hazard that makes this task non-trivial.** `try_op`'s progress rule compares `previous` — the
session's cached whole-shape defect list — against `found`, the result of the rescan. Scope only
`found` and every defect outside the region reads as having disappeared, which would both credit an
operation with clearing defects it never touched and trip the collateral-damage checks. `previous`
must be filtered to the same region before the comparison. The two sides of that comparison must
always be drawn from the same scope.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_operation_inside_a_package_re_inspects_only_its_region():
    spy = _scan_spy()
    _session(spy=spy).try_package(_package(cleared={_EDGE}))
    # every per-operation rescan is scoped; none walks the whole shape
    assert [c.index for c in spy.scoped] == [0, 0]      # the op's rescan, then the package's
    assert spy.whole_shape == []

def test_the_progress_rule_still_rolls_back_an_operation_that_cleared_nothing():
    verdict = _session(clears=False).try_package(_package(cleared={_EDGE}))
    assert [v.reason for v in verdict.verdicts] == ["target_remains"]

def test_a_defect_outside_the_region_is_not_credited_as_cleared():
    # the scoped previous/found comparison must be drawn from the same scope,
    # or an untouched defect elsewhere reads as having disappeared
    verdict = _session(elsewhere=("face:far",)).try_package(_package(cleared={_EDGE}))
    assert verdict.status == "committed"
    assert "face:far" not in verdict.reason

def test_try_op_without_a_region_is_unchanged():
    spy = _scan_spy()
    _session(spy=spy).try_op(_Op(), (_ref(_EDGE),), "small_edge", tolerance_mm=0.1)
    assert spy.scoped == [] and len(spy.whole_shape) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_package_session.py -k "region or progress or outside or unchanged" -v`
Expected: FAIL — `try_op()` got an unexpected keyword argument `within`, and the spy records whole-shape calls where scoped ones are asserted.

- [ ] **Step 3: Thread `within` through `try_op` and `_rescan`, and filter `previous` to the same scope**

Keep `try_op`'s documented step order and every existing cap. The filter on `previous` belongs
immediately before the progress rule, where the comparison happens, with a comment naming the
hazard.

- [ ] **Step 4: Run the tests and the suites that cover the unscoped path**

Run: `.venv/bin/python -m pytest tests/unit/cad/test_repair_package_session.py tests/unit/cad/test_repair_timing.py tests/native/test_native_repair_loop.py tests/native/test_native_repair_session.py -v`
Expected: PASS, with the existing whole-shape behaviour unchanged.

- [ ] **Step 5: Add the native measurement that proves the cost claim**

```python
def test_a_committed_package_pays_two_whole_shape_scans_whatever_its_plan_length():
    # the run's first scan and its final one. Every per-operation proof is scoped.
    run, spy = _run_with_spy(_two_region_part())
    assert len(spy.whole_shape) == 2
    assert len(spy.scoped) == sum(len(p.verdicts) for p in run.packages) + len(run.packages)
```

This is the assertion the plan should have carried from the start. Until it passes, the branch has
the machinery and not the saving.

- [ ] **Step 6: Commit**

```bash
git add src/meshpipeline/cad/repair/session.py tests/unit/cad/test_repair_package_session.py tests/native/test_native_repair_region_loop.py
git commit -m "feat(repair): prove an operation inside a package against its own region"
```

---

## Not In This Plan

Separate plans, each depending on this one:

- **Plan 2 - model package authoring.** Substitutes the model for `run_regions`' `author` callable, adds mid-run revision on a failed predicate, per-region model budgets, and reserved-code licensing by adding a model source to `HELD_ANSWERING_SOURCES`.
- **Plan 3 - scoped primitives.** Scoped delete, scoped tolerance-merge, then scoped local rebuild last behind its own flag, parametric by default with declared deviation.
- **Plan 4 - mesher handoff.** The disposition ledger becomes size-field zones in `engines/gmsh/fea_adapter.py`, and the OCC import-healing options that are currently never set anywhere in `src/` get switched on per region.
- **Plan 5 - evaluation.** Scoring intents, predicates and reclassification against the labelled generated-defect corpus, plus the adversarial track that holds down clean-by-relabelling.
