# Catalogue Growth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **EXECUTION WAITS.** Do not start Task 1 until the levers-fix commits (inner-wire paired-rim evidence, fail-closed `paired_rim_fingerprints`, held-entity directives, the deploy/inventory text corrections — `.superpowers/sdd/levers-fix-brief.md`) have landed on this branch. They rewrite `conservative.py`, `plan.py`, `directives.py`, `inventory.py` and `deploy.yml`, all of which this plan also edits. Task 1 Step 0 checks for them.

**Goal:** Two new bounded operations — `fill_nonplanar_hole` and `heal_self_intersections` — join the catalogue under the existing caps, each behind its own default-off flag, with the paired-rim refusal covering both fills and the corpus floor (≥ 430 repaired defects, every bore pin green) as the hard acceptance.

**Architecture:** Both ops live in `operations.py` on the slice-6 contract (targets → declare → session → caps → re-inspect). The heal is fed by a new budgeted face-level self-interference scan in `localize_scan` that runs only when its flag is on. The fills share one `FILL_OPS` name set that every guard consults; the paired-rim refusal is consumed through its interface (`plan.paired_rim_fingerprints`), extended for saddle rims only if the landed version does not already pair them. The model catalogue in `_wire.py` is pinned to `OPERATIONS` by test.

**Tech Stack:** The slice-6/7/8 repair stack (OCP/OpenCASCADE 7.8, `RepairSession`, `RulesPlanner`/`DirectedPlanner`, `llm_plan`); `devtools/validation/repair_corpus.py`; the c2217d4 flag plumbing.

**Spec:** `docs/superpowers/specs/2026-10-18-catalogue-growth-design.md` as amended in place by the builder-check rulings (`.superpowers/sdd/catalogue-growth-rulings.md`). The amended spec binds; where this plan rules on a gap the spec left, it says so under "Plan rulings".

## Global Constraints

- **Flags off is byte-for-byte today's behaviour.** With `CAD_REPAIR_FILL_NONPLANAR_HOLES=false` and `CAD_REPAIR_HEAL_SELF_INTERSECTIONS=false`: the face scan never runs, neither new op is ever proposed by the rules planner, and every scan, triage route and op log equals today's. Pinned by test (Tasks 4, 7, 8).
- **No new cap, no changed default.** `CAD_REPAIR_MAX_OPS` stays 6 and `CAD_REPAIR_STALL_LIMIT` stays 2 (only the "FIVE operations" comment is corrected). `MAX_HEAL_FACES = 4` and `PATCH_BULGE_INFLATION = 0.5` are module constants beside their ops, not settings.
- **Thresholds come from synthetic fixtures only** — bulge inflation, saddle pairing numbers, heal face bound. Never adjusted after looking at a corpus number.
- **The paired-rim refusal is consumed abstractly**: through `plan.paired_rim_fingerprints(shape, defects)` and whatever function the levers fix leaves behind it (today `conservative._paired_rims`). No task depends on roundness, inner-wire, or any other internal of how a pair is recognised.
- Settings via `settings/cad_repair.py` + `settings/inventory.py` + regenerated `docs/reference/configuration.md` (and `.env.example`, which must show no diff for `internal` vars); run `tests/unit/settings/test_configuration_documentation.py` directly.
- Host gates before any deploy: `make lint && make typecheck && make test-fast && make check-fast`; `pytest tests/unit/cad tests/unit/agents tests/unit/pipeline tests/unit/settings tests/unit/deploy tests/unit/dev -q`; native `pytest tests/native -k "repair or fixture or bore or directives or planner or minting" -q`. Known pre-existing failures stand as ledgered (bore_staging, config_parity, docker/venv dev+deploy families, builder run_python ×2); any other failure belongs to this plan.
- OCCT corpus files are never committed. Corpus-reading native tests take the path from `HEXERA_OCCT_CORPUS` (default `/tmp/cadcorpus/occt/opencascade-dataset-7.8.0/step`) and `pytest.skip` with a reason when absent.
- Commits: one declarative behaviour sentence + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Never `git add -A`; add named paths only.

## Plan rulings (gaps the amended spec left)

1. **The face scan's budget in inspection.** Ruling 7 slices the *repair run's* budget; inspection (`brep.inspect_brep`) has its own, `CAD_INSPECT_TIMEOUT_S`. Inspection therefore gets `min(0.25 × CAD_INSPECT_TIMEOUT_S, 30 s)` by the same formula, so triage can see the turbine class (the spec's Intent), and the repair session gets `min(0.25 × remaining CAD_REPAIR_TIMEOUT_S, 30 s)` per scan.
2. **A flag-off op is not proposable.** Each new op's `targets_for` returns `()` while its flag is off, and `apply` still declines with a named reason (for directives). Without the first half, a flag-off non-planar fill would be proposed after every `not_planar` decline, spend an attempt, and count toward `CAD_REPAIR_STALL_LIMIT` — changing baseline runs.
3. **"Part's original area" in the heal's `too_much_surface` decline** is the total face area of the shape the op is handed. An op never sees the upload; the cumulative cap still judges against the original.
4. **The paired-rim comparison baseline** (Goal 3) is a fresh flags-off run at the same commit, because the levers fix changes the count from the pre-fix 64. The 430 floor stays absolute.
5. **Wire-level findings stay "unclaimed".** Once the heal `answers` `self_intersection`, a wire-level finding it gives no targets for must still be recorded `no_operation_claims_it`, as today — `RulesPlanner` counts an op as claiming a defect only if it offers targets or was skipped by the run's history.

## Review Focus

1. **A saddle-rimmed cross-bore sealed by the non-planar fill** — both the guard (`FILL_OPS` membership in rules, directed and model paths, and `refuse_filling` blocking both fills) and the pairing itself (saddle fixture). (Tasks 3, 4, 6.)
2. **Flag-off parity** — no analyzer call, no new-op proposal, same refusals. A wire-level `self_intersection` still reads `no_operation_claims_it`. (Tasks 4, 7, 8.)
3. **Dedupe precedence** — a face carrying both a status-walk `self_intersection` and an analyzer one must keep the analyzer record (the one with `crossing_faces`); face-level findings go into `found` before the status walk, exactly as free bounds do for `open_shell`. (Task 7.)
4. **A timed-out face scan** returns no defects and leaves `complete_scan` as the status walk set it — never `scan_incomplete`. (Task 7.)
5. **The heal growing past its targets** — any non-target face (or an edge it does not share with a target) changing `TShape` declines `grew_beyond_targets` and returns the input shape object itself. (Task 8.)
6. **Corpus regressions the caps cannot see**: with the flags on, (a) a non-planar attempt after a `not_planar` decline can bring `STALLED` one attempt earlier, and (b) a new face-level `self_intersection` routes a file to `manual_cleanup` with review off. The floor gate and the "every baseline-kept file is kept again" check are what catch both. (Task 12.)

---

### Task 1: The two flags as settings

**Files:**
- Modify: `src/meshpipeline/settings/cad_repair.py`, `src/meshpipeline/settings/inventory.py`; regenerate `docs/reference/configuration.md` (and confirm `.env.example` unchanged)
- Test: `tests/unit/settings/test_catalogue_growth_settings.py`

**Interfaces:**
- Produces `rcfg.CAD_REPAIR_FILL_NONPLANAR_HOLES: bool` and `rcfg.CAD_REPAIR_HEAL_SELF_INTERSECTIONS: bool`, both `bool_env(..., "false")`, declared beside `CAD_REPAIR_FILL_PLANAR_HOLES` in the "CAD repair" inventory group with `kind="bool", exposure="internal"`.

- [ ] **Step 0: Precondition.** `git log --oneline -15` shows the levers-fix commits (inner-wire pairing; fail-closed `paired_rim_fingerprints`; held-entity directives). If absent, stop and report "waiting on levers fix".
- [ ] **Step 1: Failing test.**

```python
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.settings import inventory

def test_the_new_geometry_flags_exist_and_default_off():
    assert rcfg.CAD_REPAIR_FILL_NONPLANAR_HOLES is False
    assert rcfg.CAD_REPAIR_HEAL_SELF_INTERSECTIONS is False
    group = next(g for g in inventory.INVENTORY if g.title == "CAD repair")
    declared = {v.name: v for v in group.vars}
    for name in ("CAD_REPAIR_FILL_NONPLANAR_HOLES", "CAD_REPAIR_HEAL_SELF_INTERSECTIONS"):
        assert declared[name].default == "false"
        assert declared[name].kind == "bool" and declared[name].exposure == "internal"
    assert "curvature" in declared["CAD_REPAIR_FILL_NONPLANAR_HOLES"].help
    assert "only the faces" in declared["CAD_REPAIR_HEAL_SELF_INTERSECTIONS"].help

def test_the_heal_face_bound_is_not_a_setting():
    names = {v.name for g in inventory.INVENTORY for v in g.vars}
    assert "CAD_REPAIR_MAX_HEAL_FACES" not in names
```

- [ ] **Step 2: Run** `pytest tests/unit/settings/test_catalogue_growth_settings.py -q` — expected: `2 failed` (AttributeError on `CAD_REPAIR_FILL_NONPLANAR_HOLES`; the second passes once the first import succeeds, so `1 failed, 1 passed` is also acceptable).
- [ ] **Step 3: Implement** the two settings with `#:` comments in the module's voice (the fill invents curvature the customer never drew, so it does not inherit the planar flag's consent; the heal touches only the faces the kernel's analysis names), the inventory entries, then regenerate: `python -m meshpipeline.settings.inventory --reference` into the generated block of `docs/reference/configuration.md`, and `python -m meshpipeline.settings.inventory > /tmp/env.example && diff /tmp/env.example .env.example` (expected: no output).
- [ ] **Step 4: Verify** `pytest tests/unit/settings/test_catalogue_growth_settings.py tests/unit/settings/test_configuration_documentation.py tests/unit/settings/test_configuration_authority.py -q` — expected: all passed.
- [ ] **Step 5: Commit** `src/meshpipeline/settings/cad_repair.py src/meshpipeline/settings/inventory.py docs/reference/configuration.md tests/unit/settings/test_catalogue_growth_settings.py`.

### Task 2: The flags ride the cad_repair tick

**Files:**
- Modify: `.github/workflows/deploy.yml` (env export beside `CAD_REPAIR_FILL_PLANAR_HOLES` ~:1406; the `cad_repair` input comment ~:116 and description, which now name all five flags), `deploy/gcp/scripts/create-api-service.sh` (env pair + `OPT_IN_DROPPABLE`), `deploy/gcp/scripts/create-worker-fleet.sh` (`WORKER_METADATA`), `deploy/gcp/worker/startup.sh` (metadata→env pair list)
- Test: extend `tests/unit/deploy/test_cad_repair_opt_out_deploys.py`; new `tests/unit/deploy/test_catalogue_growth_flag_plumbing.py`

**Interfaces:**
- Env names `CAD_REPAIR_FILL_NONPLANAR_HOLES`, `CAD_REPAIR_HEAL_SELF_INTERSECTIONS`, each `${{ inputs.cad_repair && 'true' || '' }}`; metadata keys `cad-repair-fill-nonplanar-holes`, `cad-repair-heal-self-intersections`.

- [ ] **Step 1: Failing tests.** In the opt-out file, `FLAGS` gains both names and the ticked dict sets both to `"true"` (the existing two tests then cover them). New file:

```python
from pathlib import Path
ROOT = Path(__file__).parents[3]
NEW = {"CAD_REPAIR_FILL_NONPLANAR_HOLES": "cad-repair-fill-nonplanar-holes",
       "CAD_REPAIR_HEAL_SELF_INTERSECTIONS": "cad-repair-heal-self-intersections"}

def _text(rel): return (ROOT / rel).read_text(encoding="utf-8")

def test_each_new_flag_follows_the_planar_fill_path_through_all_four_files():
    wf, api = _text(".github/workflows/deploy.yml"), _text("deploy/gcp/scripts/create-api-service.sh")
    fleet, boot = _text("deploy/gcp/scripts/create-worker-fleet.sh"), _text("deploy/gcp/worker/startup.sh")
    for env, meta in NEW.items():
        assert f"{env}: ${{{{ inputs.cad_repair && 'true' || '' }}}}" in wf
        assert f'API_ENV_PAIRS+=("{env}=${{{env}}}")' in api
        assert f"OPT_IN_DROPPABLE+=({env})" in api
        assert f'WORKER_METADATA+=("{meta}=${{{env}}}")' in fleet
        assert f'"{meta}:{env}"' in boot
```

- [ ] **Step 2: Run** `pytest tests/unit/deploy/test_cad_repair_opt_out_deploys.py tests/unit/deploy/test_catalogue_growth_flag_plumbing.py -q` — expected: failures naming the missing strings.
- [ ] **Step 3: Implement** the four edits, copying the `CAD_REPAIR_FILL_PLANAR_HOLES` lines exactly. Update the `cad_repair` input's comment and description to say ticking enables the loop, the review, planar fill, non-planar fill and the heal.
- [ ] **Step 4: Verify** `bash -n deploy/gcp/scripts/create-api-service.sh deploy/gcp/scripts/create-worker-fleet.sh deploy/gcp/worker/startup.sh` (expected: no output), `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy.yml'))"` (expected: no output), then `pytest tests/unit/deploy -q` — expected: all passed.
- [ ] **Step 5: Commit** the four deploy files and both test files.

### Task 3: One fill set, one catalogue — neutral seams first

**Files:**
- Modify: `src/meshpipeline/cad/repair/operations.py` (add `FILL_OPS`), `src/meshpipeline/cad/repair/plan.py` (guard by membership; header comment's "FIVE"), `src/meshpipeline/cad/repair/directives.py` (fill check by membership), `src/meshpipeline/agents/repair_review/_wire.py` (add `FILL_CATALOGUE`), `src/meshpipeline/agents/repair_review/planner.py` (single-target rule by membership), `src/meshpipeline/settings/cad_repair.py` (`CAD_REPAIR_MAX_OPS` comment no longer counts operations)
- Test: `tests/unit/agents/test_catalogue_pinned_to_operations.py`; update `tests/unit/cad/test_repair_plan.py::test_exactly_one_catalogued_operation_carries_the_fill_name`

**Interfaces:**
- `operations.FILL_OPS: frozenset[str] = frozenset({FillPlanarHole.name})` (Task 4 adds the second name).
- `_wire.FILL_CATALOGUE: tuple[str, ...] = ("fill_planar_hole",)` — strings, so the agents layer does not import `cad.repair`.
- `plan.RulesPlanner.propose`: `if op.name in FILL_OPS and history.fill_refused_because: continue`. `directives.DirectedPlanner._resolve`: `if op.name in FILL_OPS and not individual:`. `planner._validate`: `if op in _wire.FILL_CATALOGUE and len(targets) != 1: return None, f"{op} takes exactly one target"`.

- [ ] **Step 1: Failing test** (the pin passes on today's names once `FILL_CATALOGUE` exists; it is red only on the missing attribute):

```python
from meshpipeline.agents.repair_review import _wire
from meshpipeline.cad.repair.operations import FILL_OPS, OPERATIONS

def test_the_model_catalogue_is_exactly_the_operation_registry():
    assert set(_wire.CATALOGUE) == {op.name for op in OPERATIONS}
    assert len(_wire.CATALOGUE) == len(OPERATIONS)

def test_every_catalogued_operation_has_a_one_line_description():
    lines = dict(l[2:].split(": ", 1) for l in _wire.CATALOGUE_TEXT.splitlines())
    assert set(lines) == set(_wire.CATALOGUE)

def test_the_model_side_fill_list_is_the_guarded_fill_set():
    assert set(_wire.FILL_CATALOGUE) == set(FILL_OPS)
    assert FILL_OPS <= {op.name for op in OPERATIONS}
```

and in `test_repair_plan.py` the fill-name test becomes: for each name in `FILL_OPS`, exactly one catalogued op carries it.
- [ ] **Step 2: Run** `pytest tests/unit/agents/test_catalogue_pinned_to_operations.py tests/unit/cad/test_repair_plan.py -q` — expected: ImportError on `FILL_OPS`.
- [ ] **Step 3: Implement** the constant and the three membership switches. No behaviour change: `FILL_OPS` holds one name.
- [ ] **Step 4: Verify** `pytest tests/unit/cad tests/unit/agents -q` — expected: all passed (the existing rim-guard and single-target tests are unchanged and green).
- [ ] **Step 5: Commit.**

### Task 4: `fill_nonplanar_hole`

**Files:**
- Modify: `src/meshpipeline/cad/repair/operations.py` (class `FillNonplanarHole`, constant `PATCH_BULGE_INFLATION`, `OPERATIONS` entry between the planar fill and the global fix, `FILL_OPS` second name), `src/meshpipeline/agents/repair_review/_wire.py` (`CATALOGUE`, `CATALOGUE_TEXT`, `CATALOGUE_LIMITS`, `FILL_CATALOGUE`), `tests/cad_fixtures.py` (`write_cylinder_cap_hole_step`)
- Test: `tests/unit/cad/test_repair_fill_nonplanar.py` (hermetic), `tests/native/test_native_repair_fill_nonplanar.py`, extend `tests/unit/cad/test_repair_directives.py` and `tests/unit/agents/test_llm_planner_agent.py`

**Interfaces:**
- `class FillNonplanarHole: name = "fill_nonplanar_hole"; answers = frozenset({DefectCode.open_shell}); scope = 6`
- `targets_for(self, defect) -> tuple[EntityRef, ...]` — identical to `FillPlanarHole.targets_for`, plus `()` while `rcfg.CAD_REPAIR_FILL_NONPLANAR_HOLES` is off (Plan ruling 2).
- `apply(self, shape, targets, *, tolerance_mm: float) -> OpOutcome`, decline order (each `evidence={"declined": <reason>, ...}`, input shape returned): `filling_disabled (CAD_REPAIR_FILL_NONPLANAR_HOLES)` → `no loop was named` → `one_loop_only` (len(targets) > 1) → `the named edge bounds no closed free loop` → `too_large (span ratio …)` → `planar` → `boundary_error` (G0 > tolerance_mm) → `patch_bulges` → `sewing_failed`. On success: `evidence` carries `AREA_DELTA`, `"patch_is_planar": False`, `"g0_error_mm"`, `"g1_achieved": bool`, `"span_mm"`, `"span_ratio"`, `"builder"` (`"BRepFill_Filling"` or `"GeomPlate"`); `added=(patch_fingerprint,)`; mapping from `mapping_from_sewing`.
- Private seams (so the hermetic tests need no kernel): `_is_planar_loop(wire) -> bool`, `_build_patch(wire, *, tolerance_mm: float) -> tuple[object, float, bool]` (face, G0 error mm, G1 achieved), `_bulges(patch, wire) -> bool`, `_sew_patch(shape, patch, *, tolerance_mm: float) -> tuple[object, object] | None` (sewn-and-promoted shape, sewing). `PATCH_BULGE_INFLATION = 0.5` — fraction of the loop box diagonal added on every side.
- Fixture `write_cylinder_cap_hole_step(path: Path, unit: str = "MM") -> Path`: a Ø40 × 100 mm cylinder whose side wall is split so one 60° × 20 mm cylindrical face can be removed, leaving one closed non-planar loop (two arcs, two lines) with span ratio well under 0.75.

- [ ] **Step 1: Fixture + red native test** (on today's catalogue, i.e. before the class exists):

```python
def test_a_curved_opening_is_closed_by_the_nonplanar_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", True)
    shape = _read(write_cylinder_cap_hole_step(tmp_path / "cap.step"))
    run = loop.run(shape, tolerance_mm=0.01)
    ops = [(v.op, v.status, v.reason) for v in run.verdicts]
    assert ("fill_planar_hole", "rolled_back", "declined:not_planar") in ops
    kept = [v for v in run.verdicts if v.kept]
    assert [v.op for v in kept] == ["fill_nonplanar_hole"]
    assert kept[0].basis in ("geometric", "kernel")
    assert not any(d.code is DefectCode.open_shell for d in run.defects)
    assert 0 < kept[0].evidence["area_delta_mm2"] < 2 * 418.9   # 20 * 60deg arc of r=20
```

plus a fixture sanity test (passes now): the fixture has exactly one closed free loop, `BRepBuilderAPI_MakeFace(wire, True).IsDone()` is False, span ratio < 0.75.
- [ ] **Step 2: Run** `pytest tests/native/test_native_repair_fill_nonplanar.py -q` — expected: the fill test FAILS with `kept == []` (only `declined:not_planar` in the log); the sanity test passes. Paste the failing assertion into the task report as the red-first proof.
- [ ] **Step 3: Commit the red-first state:** `tests/cad_fixtures.py` and the native file, with the fill test marked `@pytest.mark.xfail(strict=True, reason="fill_nonplanar_hole not yet implemented")` so the tree stays green and the marker must come off when the op lands.
- [ ] **Step 4: Hermetic failing tests** in `tests/unit/cad/test_repair_fill_nonplanar.py`, monkeypatching the private seams and `free_wire_containing`/`_span`/`_diagonal`/`_face_area`:

```python
@pytest.mark.parametrize("setup,reason", [
    ({"flag": False}, "filling_disabled"), ({"targets": ()}, "no loop was named"),
    ({"targets": (REF, REF2)}, "one_loop_only"), ({"wire": None}, "bounds no closed free loop"),
    ({"span_ratio": 0.8}, "too_large"), ({"planar": True}, "planar"),
    ({"g0": 0.02}, "boundary_error"), ({"bulges": True}, "patch_bulges"),
    ({"sew": None}, "sewing_failed")])
def test_each_decline_returns_the_input_and_names_its_reason(setup, reason, doubles):
    out = doubles(**setup).apply(SHAPE, setup.get("targets", (REF,)), tolerance_mm=0.01)
    assert out.shape is SHAPE and reason in out.evidence["declined"]

def test_the_patch_area_is_measured_before_anything_is_sewn(doubles):
    calls = doubles().calls
    doubles().apply(SHAPE, (REF,), tolerance_mm=0.01)
    assert calls.index("_face_area") < calls.index("_sew_patch")

def test_a_too_large_loop_is_declined_even_when_flat(doubles):
    out = doubles(span_ratio=0.8, planar=True).apply(SHAPE, (REF,), tolerance_mm=0.01)
    assert "too_large" in out.evidence["declined"]

def test_with_its_flag_off_the_rules_planner_never_proposes_it(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", False)
    assert FillNonplanarHole().targets_for(OPEN_LOOP_DEFECT) == ()

def test_both_fills_are_guarded_by_name():
    assert FILL_OPS == {"fill_planar_hole", "fill_nonplanar_hole"}
```

In `test_repair_directives.py`: an LLM-sourced and a group-engineer `fill_nonplanar_hole` directive on a paired rim (monkeypatched `paired_rim_fingerprints`) are skipped `fill_refused`; an individual engineer one plays. In `test_repair_plan.py`: after `history.refuse_filling(...)` the rules planner proposes neither fill. In `test_llm_planner_agent.py`: a two-target `fill_nonplanar_hole` proposal declines `"fill_nonplanar_hole takes exactly one target"`.
- [ ] **Step 5: Run** `pytest tests/unit/cad/test_repair_fill_nonplanar.py tests/unit/cad/test_repair_directives.py tests/unit/cad/test_repair_plan.py tests/unit/agents -q` — expected: ImportError / failures on the new names.
- [ ] **Step 6: Implement** the class (docstring: what it may touch, what it declares, why each decline exists, and the `BRepFill_Filling` vs `GeomPlate` choice justified), add it to `OPERATIONS` and `FILL_OPS` IN THE SAME COMMIT, add the `_wire` entries (one-liner e.g. `"- fill_nonplanar_hole: cap a small curved hole with a constrained patch"`; `CATALOGUE_LIMITS` names it), remove the xfail marker.
- [ ] **Step 7: Verify** `pytest tests/unit/cad tests/unit/agents -q` (expected: all passed) and `pytest tests/native/test_native_repair_fill_nonplanar.py tests/native -k "repair or fixture" -q` (expected: fill test passes; no new native failures beyond the ledger).
- [ ] **Step 8: Commit.**

### Task 5: Flag-off parity for the fill on real parts

**Files:**
- Test: extend `tests/native/test_native_repair_fill_nonplanar.py`

**Interfaces:** none new; this pins Plan ruling 2 on the kernel.

- [ ] **Step 1: Test** (expected to pass immediately — it pins, it does not drive):

```python
def test_with_the_nonplanar_flag_off_the_run_is_todays_run(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_NONPLANAR_HOLES", False)
    shape = _read(write_cylinder_cap_hole_step(tmp_path / "cap.step"))
    with_op = loop.run(shape, tolerance_mm=0.01)
    without = loop.run(shape, planner=RulesPlanner(ops=tuple(o for o in OPERATIONS
                       if o.name != "fill_nonplanar_hole")), tolerance_mm=0.01)
    assert [v.to_dict() for v in with_op.verdicts] == [v.to_dict() for v in without.verdicts]
    assert with_op.refusals == without.refusals and with_op.stopped_because == without.stopped_because
```

Repeat for `write_open_shell_step` and `write_missing_bore_wall_step`.
- [ ] **Step 2: Run** `pytest tests/native/test_native_repair_fill_nonplanar.py -q` — expected: all passed. If red, the fix is in `targets_for`'s flag gate, not the test.
- [ ] **Step 3: Commit.**

### Task 6: Saddle rims pair — the paired-rim refusal sees curved walls

**Files:**
- Modify: `tests/cad_fixtures.py` (`write_cross_drilled_cylinder_step`); `src/meshpipeline/cad/repair/conservative.py` ONLY IF Step 2 is red
- Test: `tests/native/test_native_repair_plan_bore_saddle.py`; hermetic `tests/unit/cad/test_repair_saddle_pairing.py` (only if the branch is written)

**Interfaces:**
- Fixture `write_cross_drilled_cylinder_step(path: Path, unit: str = "MM") -> Path`: Ø40 × 100 mm cylinder (axis x), Ø8 cross-bore along z through the axis at x = 50, bore wall face(s) removed — two saddle-shaped closed free loops on the lateral face. Also `write_two_unrelated_curved_holes_step`: the same cylinder with two Ø8 windows 40 mm apart along x (not on one axis) — must NOT pair.
- If needed: `conservative._paired_nonplanar_rims(loops: list) -> set[int]`, called from the landed paired-rim function for every loop pair where at least one loop is non-planar. Best-fit axis = smallest principal component of `_loop_points(wire)`; mean radius = mean distance from the centroid perpendicular to that axis. Constants (fixed from the fixtures, stated with their reasons): `_SADDLE_AXIS_PARALLEL = 0.98` (|dot|), `_SADDLE_AXIS_OFFSET = 0.5` (centroid offset ⟂ axis / smaller radius), `_SADDLE_RADIUS_RATIO = 2.5`, gap along axis in `[1e-3, 40]` × larger radius — each erring toward pairing.

- [ ] **Step 1: Red-first native test** (the non-planar fill exists since Task 4):

```python
def test_a_cross_drilled_cylinders_saddle_rims_are_never_filled(tmp_path, monkeypatch):
    for flag in ("CAD_REPAIR_FILL_PLANAR_HOLES", "CAD_REPAIR_FILL_NONPLANAR_HOLES"):
        monkeypatch.setattr(rcfg, flag, True)
    shape = _read(write_cross_drilled_cylinder_step(tmp_path / "x.step"))
    found, _ = localize_scan(shape)
    rims = [d for d in found if d.code is DefectCode.open_shell
            and (d.measurements or {}).get("boundary_edges")]
    assert len(rims) == 2
    assert paired_rim_fingerprints(shape, found) == {r.fingerprint for r in rims}
    run = loop.run(shape, tolerance_mm=0.01)
    assert not [v for v in run.verdicts if v.kept and v.op in FILL_OPS]
    assert sum(1 for r in run.refusals if r.get("reason") == "paired_rim") == 2

def test_two_curved_holes_off_one_axis_do_not_pair(tmp_path):
    shape = _read(write_two_unrelated_curved_holes_step(tmp_path / "w.step"))
    found, _ = localize_scan(shape)
    assert paired_rim_fingerprints(shape, found) == frozenset()
```

Also re-run the existing bore pins (`write_missing_bore_wall_step` pairs; loose box faces do not; the levers-fix hex/obround/square/deep/spot-face/16-gon pins).
- [ ] **Step 2: Run** `pytest tests/native/test_native_repair_plan_bore_saddle.py tests/native/test_native_repair_plan_bore.py -q`. Expected on the landed guard: the saddle test FAILS showing a kept `fill_nonplanar_hole` (the hole sealed) — paste it as the red-first proof. If it PASSES, the inner-wire refusal already pairs saddles: say so in the report, keep the test as the pin, skip Steps 3–4.
- [ ] **Step 3: Hermetic failing tests** for `_paired_nonplanar_rims` over synthetic point loops (patch `_loop_points`): two saddles on one axis pair; same saddles offset ⟂ axis by 1.0 × radius do not; radius ratio 2.4 pairs, 2.6 does not; a planar+saddle pair on one axis pairs. Run — expected: ImportError. Then implement the branch, docstring stating every number and that a false pair costs one missed repair while a missed pair costs a customer's hole.
- [ ] **Step 4: Verify** both native files and `pytest tests/unit/cad -q` — expected: all passed, every existing bore pin still green.
- [ ] **Step 5: Commit** (fixtures + tests; `conservative.py` only if changed).

### Task 7: Face-level self-intersection detection, budgeted and gated

**Files:**
- Modify: `src/meshpipeline/cad/repair/localize.py`, `src/meshpipeline/cad/repair/session.py`, `src/meshpipeline/cad/repair/loop.py`, `src/meshpipeline/cad/repair/brep.py`
- Test: `tests/unit/cad/test_repair_face_scan.py` (hermetic), `tests/native/test_native_repair_face_scan.py`, `tests/native/_face_scan_baseline.json` (captured pre-change in Step 2)

**Interfaces:**
- `localize.FACE_SCAN_FRACTION = 0.25`, `localize.FACE_SCAN_CAP_S = 30.0`; `face_scan_budget_s(remaining_s: float) -> float` = `max(0.0, min(FACE_SCAN_FRACTION * remaining_s, FACE_SCAN_CAP_S))`.
- `_bounded(fn, *args, budget_s: float)` → `fn(*args)`'s result, or `None` on expiry or any exception (logged at info). A daemon thread abandoned on expiry, plus the kernel's user-break progress indicator if OCP exposes one, so the analysis also stops rather than only being ignored.
- `_kernel_face_pairs(shape) -> list[tuple[int, int]]` — 1-based face-index pairs from the kernel analyzer (builder's choice of `BOPAlgo_ArgumentAnalyzer` self-interference vs `BRepAlgoAPI_Check`; face-typed faulty shapes only).
- `_components(pairs: list[tuple[int, int]]) -> dict[int, list[int]]` — pure: each face index mapped to the sorted rest of its connected component in the pair graph.
- `face_self_intersections(shape, *, budget_s: float) -> list[EntityDefect]` — `_bounded(_kernel_face_pairs, shape, budget_s=budget_s)`; `None` ⇒ `[]`. Each face in a component becomes one `EntityDefect(code=self_intersection, severity=error, entity_type="face", kernel_status="SelfInterference", measurements={"crossing_face_indices": _components(...)[i]}, location=_centroid_and_box(face))`.
- `localize_scan(shape, *, valid: bool | None = None, face_scan_s: float = 0.0)` — `face_scan_s <= 0` never calls the analyzer. Face-level findings are extended into `found` AFTER free bounds and BEFORE the status walk. After `_stamped`, a linking pass rewrites each face finding's measurements to add `"crossing_faces": [stamped fingerprints of its partners]`. The returned completeness is the status walk's alone.
- `RepairSession.__init__(self, shape, *, measurer=_measure, scanner=None, remaining_s: Callable[[], float] | None = None)` — `scanner=None` means a closure that looks the module-level `_localize_scan` up AT CALL TIME and runs `localize_scan(s, face_scan_s=face_scan_budget_s(remaining_s()) if rcfg.CAD_REPAIR_HEAL_SELF_INTERSECTIONS else 0.0)` (with `remaining_s` defaulting to `lambda: rcfg.CAD_REPAIR_TIMEOUT_S`). Injected scanners keep the one-argument `scanner(shape)` call.
- `loop.run`: `started = clock()` moves above session construction; a default-built session gets `remaining_s=lambda: limits.timeout_s - (clock() - started)`; an injected session with `remaining_s is None` gets the same assigned.
- `brep.inspect_brep`: passes `face_scan_s=face_scan_budget_s(rcfg.CAD_INSPECT_TIMEOUT_S) if rcfg.CAD_REPAIR_HEAL_SELF_INTERSECTIONS else 0.0` (Plan ruling 1).

- [ ] **Step 1: Hermetic failing tests:**

```python
@pytest.mark.parametrize("remaining,expected", [(200.0, 30.0), (120.0, 30.0), (40.0, 10.0),
                                                (0.0, 0.0), (-5.0, 0.0)])
def test_the_budget_is_a_quarter_of_what_remains_capped_at_thirty(remaining, expected):
    assert face_scan_budget_s(remaining) == expected

def test_a_slow_analysis_is_abandoned_not_waited_for():
    def slow(): time.sleep(0.5); return [(1, 2)]
    started = time.monotonic()
    assert _bounded(slow, budget_s=0.05) is None
    assert time.monotonic() - started < 0.3

def test_a_raising_analysis_is_silence():
    def boom(): raise RuntimeError("kernel")
    assert _bounded(boom, budget_s=1.0) is None

def test_a_non_positive_budget_never_calls_the_analysis():
    called = []
    assert _bounded(lambda: called.append(1), budget_s=0.0) is None and called == []

def test_pairs_close_into_components_and_each_face_lists_its_partners():
    assert _components([(1, 2), (2, 3), (5, 6)]) == {1: [2, 3], 2: [1, 3], 3: [1, 2], 5: [6], 6: [5]}
    assert _components([]) == {}

def test_with_the_heal_flag_off_the_session_never_asks_for_a_face_scan(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    seen = []
    monkeypatch.setattr(session_mod, "_localize_scan", lambda s, **kw: seen.append(kw) or ((), True))
    RepairSession(object(), measurer=lambda s: MEASURE).defects()
    assert seen == [{"face_scan_s": 0.0}]

def test_with_the_flag_on_the_session_asks_with_the_remaining_budget_slice(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    seen = []
    monkeypatch.setattr(session_mod, "_localize_scan", lambda s, **kw: seen.append(kw) or ((), True))
    RepairSession(object(), measurer=lambda s: MEASURE, remaining_s=lambda: 40.0).defects()
    assert seen == [{"face_scan_s": 10.0}]
```

- [ ] **Step 2: Capture the flag-off baseline, then run red.** Before touching `localize.py`, dump `[d.to_dict() for d in localize_scan(shape)[0]]` for every committed repair fixture (`write_open_shell_step`, `write_missing_bore_wall_step`, `write_small_edge_step`, `write_loose_faces_step`, `write_mixed_assembly_step`, `write_cylinder_cap_hole_step`, `write_cross_drilled_cylinder_step`) into `tests/native/_face_scan_baseline.json`. Then `pytest tests/unit/cad/test_repair_face_scan.py -q` — expected: ImportError on `face_scan_budget_s`.
- [ ] **Step 3: Implement** `localize.py` (constants, both functions, the `localize_scan` parameter, ordering, linking pass; docstrings say the scan runs only with the heal enabled and why a timeout is silence rather than incompleteness), then `session.py`, `loop.py`, `brep.py`.
- [ ] **Step 4: Native tests:**

```python
def test_flag_off_scans_are_identical_to_todays(tmp_path):   # every committed repair fixture
    for write in FIXTURES:
        shape = _read(write(tmp_path / f"{write.__name__}.step"))
        assert localize_scan(shape) == localize_scan(shape, face_scan_s=0.0)
        assert [d.to_dict() for d in localize_scan(shape)[0]] == BASELINE[write.__name__]

def test_a_timed_out_face_scan_keeps_the_status_walks_completeness(tmp_path, monkeypatch):
    shape = _read(write_open_shell_step(tmp_path / "o.step"))
    monkeypatch.setattr(localize, "_kernel_face_pairs", lambda s: time.sleep(1) or [(1, 2)])
    found, complete = localize_scan(shape, face_scan_s=0.05)
    assert complete is True and not [d for d in found if d.kernel_status == "SelfInterference"]

def test_the_analyzer_record_wins_dedupe_over_a_status_walk_record(tmp_path, monkeypatch):
    shape = _read(write_open_shell_step(tmp_path / "o.step"))
    monkeypatch.setattr(localize, "_kernel_face_pairs", lambda s: [(1, 2)])
    walk = EntityDefect(code=DefectCode.self_intersection, severity=DefectSeverity.error,
                        entity_type="face", entity_index=1, message="walk",
                        kernel_status="SelfIntersectingWire")
    monkeypatch.setattr(localize, "_status_defects", lambda s, **kw: ([walk], True))
    found, _ = localize_scan(shape, valid=False, face_scan_s=5.0)
    survivor = next(d for d in found if d.entity_type == "face" and d.entity_index == 1
                    and d.code is DefectCode.self_intersection)
    assert survivor.measurements["crossing_faces"]
```

`BASELINE` is `tests/native/_face_scan_baseline.json` from Step 2, captured from the pre-change code.
- [ ] **Step 5: Verify** `pytest tests/unit/cad tests/unit/pipeline -q` and `pytest tests/native/test_native_repair_face_scan.py tests/native -k "repair or fixture" -q` — expected: all passed (ledgered failures excepted).
- [ ] **Step 6: Commit.**

### Task 8: `heal_self_intersections`

**Files:**
- Modify: `src/meshpipeline/cad/repair/operations.py` (class `HealSelfIntersections`, `MAX_HEAL_FACES = 4`, `OPERATIONS` entry after the non-planar fill), `src/meshpipeline/agents/repair_review/_wire.py` (`CATALOGUE`, `CATALOGUE_TEXT`, `CATALOGUE_LIMITS`), `src/meshpipeline/cad/repair/plan.py` (claim counting, Plan ruling 5)
- Test: `tests/unit/cad/test_repair_heal.py` (hermetic), extend `tests/unit/cad/test_repair_plan.py`

**Interfaces:**
- `class HealSelfIntersections: name = "heal_self_intersections"; answers = frozenset({DefectCode.self_intersection}); scope = 7`
- `targets_for(self, defect) -> tuple[EntityRef, ...]`: `()` unless the flag is on, `entity_type == "face"`, a fingerprint, and non-empty `measurements["crossing_faces"]` with matching `crossing_face_indices`; then the defect's face ref followed by one ref per partner (fingerprint from `crossing_faces`, index from `crossing_face_indices`), de-duplicated.
- `apply(self, shape, targets, *, tolerance_mm: float) -> OpOutcome`, decline order (input shape object returned each time): `healing_disabled (CAD_REPAIR_HEAL_SELF_INTERSECTIONS)` → `not_isolated` (no target resolves; more than `MAX_HEAL_FACES` targets; or the pre-analysis over the target faces finds no crossing among them — ruling 6 for directives) → `multi_body` (targets owned by more than one solid, or more than one shell when the shape has no solid) → `too_much_surface` (|declared area| > `CAD_REPAIR_MAX_AREA_CHANGE_RATIO` × total face area of the shape handed in, Plan ruling 3) → `grew_beyond_targets` → `crossing_remains`. On success: `evidence={AREA_DELTA: rebuilt − original area, "faces_targeted", "faces_rebuilt", "precision_mm"}`, `added` = fingerprints of rebuilt faces whose box changed, mapping from `mapping_from_context` over the `splice` context.
- Private seams: `_analyze_faces(shape, faces) -> list[tuple[int, int]]`, `_rebuild_face(face, *, tolerance_mm: float) -> object` (builder's choice among `ShapeFix_Wire`/`ShapeFix_Face`/`ShapeFix_IntersectionWire`; removal modes off, as in `FixFaceBoundary`), `_owners(shape, faces) -> set`, `_outside_identities(shape, target_faces) -> frozenset` (TShapes of every non-target face and of every edge not shared with a target), and the pure `_grew(before: frozenset, after: frozenset) -> bool` = `not before <= after`.
- `plan.RulesPlanner.propose`: `claimed = True` only when the op's `targets_for` is non-empty or the op was skipped by `was_ineffective` / the fill refusal.

- [ ] **Step 1: Hermetic failing tests:**

```python
def test_it_claims_only_face_findings_that_carry_crossing_evidence(flag_on):
    heal = HealSelfIntersections()
    assert heal.targets_for(WIRE_LEVEL_SELF_INTERSECTION) == ()
    assert heal.targets_for(FACE_FINDING_WITHOUT_CROSSINGS) == ()
    refs = heal.targets_for(FACE_FINDING_WITH_PARTNERS)        # face 3, partners 7 and 9
    assert [(r.entity_type, r.entity_index) for r in refs] == [("face", 3), ("face", 7), ("face", 9)]

def test_with_its_flag_off_it_offers_no_targets(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    assert HealSelfIntersections().targets_for(FACE_FINDING_WITH_PARTNERS) == ()

@pytest.mark.parametrize("setup,reason", [
    ({"flag": False}, "healing_disabled"), ({"resolved": 0}, "not_isolated"),
    ({"n_targets": MAX_HEAL_FACES + 1}, "not_isolated"), ({"pre_pairs": []}, "not_isolated"),
    ({"owners": 2}, "multi_body"), ({"area_delta_ratio": 0.03}, "too_much_surface"),
    ({"grew": True}, "grew_beyond_targets"), ({"post_pairs": [(3, 7)]}, "crossing_remains")])
def test_each_decline_returns_the_input_shape_itself(setup, reason, heal_doubles):
    out = heal_doubles(**setup).apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert out.shape is SHAPE and out.evidence["declined"].startswith(reason)

def test_the_area_is_declared_from_measurements_taken_before_the_splice(heal_doubles):
    d = heal_doubles(); d.apply(SHAPE, TARGETS, tolerance_mm=0.01)
    assert d.calls.index("_face_area:rebuilt") < d.calls.index("splice")

def test_grew_is_any_outside_identity_lost():
    assert _grew(frozenset({"a", "b"}), frozenset({"a", "b", "new"})) is False
    assert _grew(frozenset({"a", "b"}), frozenset({"a", "c"})) is True

def test_the_heal_face_bound_is_four():
    assert MAX_HEAL_FACES == 4
```

In `test_repair_plan.py`:

```python
def test_a_wire_level_self_intersection_is_still_reported_unclaimed(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    planner = RulesPlanner()                 # real catalogue
    history = RunHistory()
    assert planner.propose([WIRE_LEVEL_SELF_INTERSECTION], history, shape=None) is None
    assert [r["reason"] for r in history.refusals] == ["no_operation_claims_it"]

def test_a_held_self_intersection_is_not_proposed_by_the_rules_planner():
    planner = RulesPlanner(reserved=REVIEW_RESERVED_CODES)
    assert planner.propose([FACE_FINDING_WITH_PARTNERS], RunHistory(), shape=None) is None
```

and in `test_repair_directives.py`: an engineer `heal_self_intersections` directive on a held face plays; an LLM one is skipped `reserved_for_review` (review on) and plays (review off).
- [ ] **Step 2: Run** `pytest tests/unit/cad/test_repair_heal.py tests/unit/cad/test_repair_plan.py tests/unit/cad/test_repair_directives.py -q` — expected: ImportError on `HealSelfIntersections`.
- [ ] **Step 3: Implement** the class (docstring: may touch only target faces and edges they share; declares the area delta; why each decline exists, especially why `grew_beyond_targets` is enforced rather than hoped for), the `plan.py` claim tweak, `OPERATIONS` and `_wire` entries (one-liner e.g. `"- heal_self_intersections: re-trim only the faces the kernel says cross each other"`).
- [ ] **Step 4: Verify** `pytest tests/unit/cad tests/unit/agents tests/unit/pipeline -q` — expected: all passed, including Task 3's catalogue pin with seven names.
- [ ] **Step 5: Commit.**

### Task 9: The heal on the real kernel

**Files:**
- Modify: `tests/cad_fixtures.py` only if a synthetic fixture proves real
- Test: `tests/native/test_native_repair_heal.py`

**Interfaces:** none new. Synthetic first: the builder tries to forge a face-level self-interference the analyzer reports (e.g. two faces of one shell sewn so they pass through each other). Kept only if `face_self_intersections` locates it AND the heal removes it on re-inspection; otherwise deleted and the report says so. The corpus proof is always present.

- [ ] **Step 1: Tests** (red until Tasks 7–8 work on the kernel; skipped with a reason when the corpus is absent):

```python
TURBINE = Path(os.environ.get("HEXERA_OCCT_CORPUS", DEFAULT_CORPUS)) / "ec_turbine-A.stp"

@pytest.mark.skipif(not TURBINE.exists(), reason="OCCT corpus not on disk (licence: never committed)")
def test_the_turbine_crossing_is_located_and_healed_by_an_engineer_directive(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    shape = _read(TURBINE)
    found, _ = localize_scan(shape, face_scan_s=30.0)
    located = [d for d in found if d.code is DefectCode.self_intersection
               and d.measurements.get("crossing_faces")]
    assert located, "the face scan located no crossing on ec_turbine-A"
    target = located[0]
    directive = {"op": "heal_self_intersections", "defect_code": "self_intersection",
                 "defect_fingerprint": target.fingerprint,
                 "targets": [target.fingerprint, *target.measurements["crossing_faces"]],
                 "source": "engineer", "item_id": "turbine-proof"}
    run = loop.run(shape, planner=DirectedPlanner([directive],
                   reserved=REVIEW_RESERVED_CODES), tolerance_mm=0.01)
    verdict = run.verdicts[0]
    assert verdict.op == "heal_self_intersections"
    # EITHER kept with the target gone and an honest basis, OR a named decline, OR judged and
    # rolled back by the caps / progress rule - never a silent no-op
    if verdict.kept:
        assert verdict.basis in ("geometric", "kernel")
        assert target.fingerprint not in {d.fingerprint for d in run.defects
                                          if d.code is DefectCode.self_intersection}
    elif verdict.reason.startswith("declined:"):
        assert verdict.reason[len("declined:"):].split(" ")[0] in {
            "not_isolated", "multi_body", "too_much_surface", "grew_beyond_targets", "crossing_remains"}
    else:
        assert verdict.reason.startswith(("cap:", "target_remains", "new_defect"))

def test_a_heal_on_a_sound_part_is_declined_not_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", True)
    shape = _read(write_step(tmp_path / "box.step", "MM"))
    out = HealSelfIntersections().apply(shape, (face_ref(shape, 1),), tolerance_mm=0.01)
    assert out.shape is shape and out.evidence["declined"].startswith("not_isolated")
```

- [ ] **Step 2: Run** `pytest tests/native/test_native_repair_heal.py -q -rs` — expected: passed (or the turbine test skipped with the licence reason). The report states which branch the turbine took — kept, or which decline — verbatim; a decline is an honest outcome, not a failure to hide.
- [ ] **Step 3: Commit.**

### Task 10: The corpus tool sees both operations and corroborates heals

**Files:**
- Modify: `devtools/validation/repair_corpus.py`
- Test: `tests/unit/dev/test_repair_corpus_catalogue.py`

**Interfaces:**
- Header line adds `FILL_NONPLANAR_HOLES=… HEAL_SELF_INTERSECTIONS=…`.
- `heal_corroborated(shape) -> bool | None` — tessellate the repaired shape to a temp STL and return `surface_checks.self_intersection_report(path) is None` (`None` when the check cannot run). Called per row only when a `heal_self_intersections` op was kept; stored as `row["heal_corroborated"]`. Summary section `== heals corroborated by the triangle check ==` prints `corroborated / kept` and lists uncorroborated files. A heal counts toward the turbine class only when corroborated (spec call 6).
- `summarise_catalogue(rows) -> dict` — the pure core of the existing kept/rolled-back/declined tally and "never exercised" list, extracted so it is testable; `main` prints from it unchanged.

- [ ] **Step 1: Failing tests:**

```python
def test_an_operation_the_corpus_never_reached_is_listed_not_omitted():
    out = summarise_catalogue([{"operations": [{"op": "fill_planar_hole", "status": "kept"}]}])
    assert {"fill_nonplanar_hole", "heal_self_intersections"} <= set(out["never_exercised"])

def test_a_declined_only_new_op_is_reported_as_proposed_but_never_ran():
    rows = [{"operations": [{"op": "heal_self_intersections", "status": "rolled_back",
                             "reason": "declined:not_isolated"}]}]
    out = summarise_catalogue(rows)
    assert out["tally"]["heal_self_intersections"]["declined"] == 1
    assert "heal_self_intersections" in out["only_declined"]

def test_corroboration_reads_the_triangle_check(monkeypatch):
    monkeypatch.setattr(corpus, "_tessellate_to_stl", lambda shape, path: path)
    monkeypatch.setattr(surface_checks, "self_intersection_report", lambda p, **kw: None)
    assert corpus.heal_corroborated(object()) is True
    monkeypatch.setattr(surface_checks, "self_intersection_report", lambda p, **kw: {"pairs": 3})
    assert corpus.heal_corroborated(object()) is False
```

- [ ] **Step 2: Run** `pytest tests/unit/dev/test_repair_corpus_catalogue.py -q` — expected: ImportError on `summarise_catalogue`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Verify** `pytest tests/unit/dev -q` — expected: all passed (including `test_repair_corpus_planner.py`); `python devtools/validation/repair_corpus.py --limit 2 --loop <fixtures dir>` prints both new flags in the header and both names under "never exercised" or the tally.
- [ ] **Step 5: Commit.**

### Task 11: Host gates, native tier, personal environment

No source changes unless a gate fails (then fix in the owning task's files and commit separately).

- [ ] **Step 1: Host gates** — `make lint && make typecheck && make test-fast && make check-fast`; expected: each exits 0. Then the unit and native commands from Global Constraints; expected: passes apart from the ledgered failures, listed by name in the report.
- [ ] **Step 2: Deploy** to the task's own slug: `gh workflow run deploy.yml --ref "$(git branch --show-current)" -f slug=pranav -f app=true -f console=true -f fleet=true -f cad_repair=true`, then `gh run watch` — expected: success.
- [ ] **Step 3: Prove the flags reached both sides:** `gcloud run services describe dev-pranav-api --region us-central1 --format=json | jq '.spec.template.spec.containers[0].env[] | select(.name|test("CAD_REPAIR_(FILL_NONPLANAR_HOLES|HEAL_SELF_INTERSECTIONS)"))'` — expected: both `"true"`; and the fleet template metadata carries `cad-repair-fill-nonplanar-holes=true` and `cad-repair-heal-self-intersections=true`.
- [ ] **Step 4: Exercise it:** upload the cylinder-cap fixture through the console (or the API the slice-7 recorder uses) and read the run's op log — the worker's `repair loop: stopped=… kept=…` line and the job's repair record — e.g. `gcloud logging read 'textPayload:"repair loop: stopped"' --freshness 30m --limit 50` narrowed to the `dev-pranav` fleet. Expected: the job's record shows `fill_nonplanar_hole` with status `kept`. The report names the job id and quotes the line it read; "deployed" alone is not the claim.

### Task 12: Corpus validation — the floor gate

Evidence only. Run by the controller or one subagent with these exact commands. Settings identical to the discriminator run (`CAD_REPAIR_MAX_DEVIATION_RATIO=0.001`, `CAD_REPAIR_MAX_TOLERANCE_MM=0.1`, `CAD_REPAIR_FILL_PLANAR_HOLES=true`, loop on, planner off).

- [ ] **Step 1: Fresh baseline at this commit, new flags off:** `CAD_REPAIR_ENABLED=true CAD_REPAIR_FILL_PLANAR_HOLES=true python devtools/validation/repair_corpus.py /tmp/cadcorpus/occt/opencascade-dataset-7.8.0/step --loop --json /tmp/planner-evidence/catalogue-baseline.json | tee /tmp/planner-evidence/catalogue-baseline.log`.
- [ ] **Step 2: With the new ops:** same command plus `CAD_REPAIR_FILL_NONPLANAR_HOLES=true CAD_REPAIR_HEAL_SELF_INTERSECTIONS=true`, writing `catalogue-growth.json` / `.log`.
- [ ] **Step 3: Gate** (a short script over the two JSON files; quote its output verbatim):
  - aggregate repaired defects in Step 2 **≥ 430** — below that: STOP, report, commit nothing claiming success;
  - every native bore/pairing pin passed in Task 11 — any red: STOP;
  - `paired_rim` refusals Step 2 ≥ Step 1 (Plan ruling 4), and no file with a `paired_rim` refusal in Step 1 has a kept `fill_*` op in Step 2;
  - every file with a kept op in Step 1 has a kept op in Step 2, and every promoted file's final cumulative check passed;
  - both new op names appear in the tally or in "never exercised"; heal corroboration count printed.
  - Only if the aggregate is **> 430** does the report claim "strictly more". At exactly 430 the report says the floor held and the claim is not made.
- [ ] **Step 4: Commit the evidence** — the two JSON/log reports copied beside the demo evidence (the same location as the discriminator reports, never the corpus files), plus a before/after table: repaired defects (by kind), cap violations, `paired_rim` refusals, kept/rolled-back/declined per op.
- [ ] **Step 5: Update the PR** with a catalogue-growth section: both ops, the detection, the guard extension (or the note that the inner-wire guard already paired saddles), the flag plumbing, the corpus table, the personal-environment job and log line from Task 11.

---

## Self-review (at writing time)

- **Spec coverage:** non-planar fill — span bound, planar decline, single target, bulge (ruling 5), G0/G1, own flag (T4); paired-rim refusal over both fills (T3/T4) and saddle evidence (T6); heal — own flag (T1/T2, ruling 4), crossing_faces-only claim (T8, ruling 6), isolation bound as constant (T8, ruling 1), multi_body / crossing_remains / grew_beyond_targets / too_much_surface (T8), kernel proof or honest decline on ec_turbine-A via an engineer directive (T9); detection — kernel analyzer, flag-gated (ruling 3), budget slice and silent skip (ruling 7), dedupe precedence (T7); model catalogue + drift pin (T3, T4, T8); mesher corroboration (T10); corpus tallies list both (T10); floor gate and pins (T12, ruling 2); personal env (T11); hold unchanged (T8 tests). Non-goals respected: no cap or default changed, no `fix_shape_global` change, no corpus files committed.
- **Step scan:** every test step carries assertions; every code step names exact signatures; every run step names its command and expected result; no "TBD" or "similar to".
- **Type consistency:** `FILL_OPS: frozenset[str]` (T3) is consumed in T4/T6 and mirrored by `_wire.FILL_CATALOGUE` under a pin; `face_scan_budget_s` / `face_self_intersections` / `localize_scan(face_scan_s=)` (T7) are consumed by the session (T7), the heal's targets via `crossing_faces` + `crossing_face_indices` (T8), and the native proof (T9); `RepairSession(remaining_s=)` defined and wired in T7 only.
- **Review Focus:** each item names the task whose test owns it.
- **Proportion:** twelve tasks, each one commit (T4 two, for the red-first fixture); the spec's two ops and the detection carry most of the weight, plumbing and evidence are short.
