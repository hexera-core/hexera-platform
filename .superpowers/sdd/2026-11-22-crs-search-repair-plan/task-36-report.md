# Task 36 report: why the search found zero verdict candidates on an open shell

Branch `sdd/task-36`, base `33c57842`. Head = the commit carrying this report (`git rev-parse HEAD`; src fix at `a344c5b2`).
No model, GCP or VM used. Nothing killed. Native runs on `meshpipeline-mesh:native`, unit on `meshpipeline-validation:local`.

## Diagnosis (before the fix)

**No candidate was GENERATED. It was never the judge.** Chain, traced on the 10 mm box with one face deleted:

1. Input verdict hard findings: `no_solids` (entities []) and `open_shell` (entities `['H1']`, free_edges 4). Mesher members fail `no_volume` with no `located`.
2. `sites.find_sites` resolves a finding's entity names to stable ids with `_kernel_entities`, whose own docstring says "Shells (H) have none". `H1` resolves to nothing, so the code did `whole_part["unlocated"].add("valid")`: **the finding's kind (`open`, `invalid`) was thrown away** and the only site was `S@unlocated`, no entities.
3. `DetGenerator._sweep` has no rows for kind `unlocated` (test_search asserts this deliberately), so `propose()` returned `[]`. No candidate, no op call, no judge call. `exhausted` with 0 verdict candidates.
4. `applies_to` was never the problem: `open` sites do match sew / make_volume / orient_and_close once the site is `open`.

Measured (`/tmp/t36/drive.py`, real judge): sites at base = `[S@unlocated]` for BOTH `h_mm=1.0` and `h_mm=1.732`. **The hard-coded `H_MM = 1.0` explains none of the empty result**: no geometric node or point exists, so `_geometric_sites` (point radius, `PROXIMITY_FACTOR*h`, `CLUSTER_FACTOR*h`) never runs. The wrong h_mm is a separate defect (Task 35), real for parts that DO get located sites.

`op_sew` rejecting `0.05` and `v4_fix_poles` changing nothing are NOT the search's: `0.05` comes from `stages.py:82` (`"shells": tol_ladder [0.001, 0.01, 0.05]`, the **staged** arm); `fix_poles` is a staged stage. The search never called sew at all. `fix_poles` doing nothing on a poleless box is expected; I did not look further.

## Fix (one line, `sites.py`)

A hard finding that cannot be placed now keeps its kind: `whole_part[_HARD_KINDS[finding.code]].add("valid")` instead of `"unlocated"`. Sites on the box become `S@open`, `S@invalid`, `S@unlocated` (the last from the meshers, still empty-sweep).

## First candidates to reach a verdict (open box, `search_det`, real judge)

`verdict_candidates = 2`, reason `exhausted`, `clean False`. Trace:
- `sew {"tol_ladder": [1e-4, 1e-3, 1e-2]}` targets [] : **declined** (`sewing ... left 4 free edges (input had 4); no rung improved the shape`). Honest negative.
- `make_volume {}` targets [] : **declined** (`MakerVolume found no closed volume`). Honest negative.
- **`orient_and_close {}` targets [] : reached a verdict**, score `[0, 0, -1.0, -0.0, 0]`, classed `duplicate` of the start (no change). Second one (from the `invalid` site) same.
Nothing converted to a pass. `infrastructure_failed False`.

## The bigger finding: no op in the library repairs THIS defect

The box's missing face is a loop spanning 10 mm = 57.7 % of the 17.32 mm diagonal. `fill_loop` (the only op that adds a face, `site_kinds={"open"}`) refuses beyond 5 % of the diagonal: I called it on all 12 box edges, every one `DECLINED too_large`. sew and make_volume cannot invent a face. **So `face_delete` of a whole box face is outside the op library's fidelity bound by design; it is a poor canonical positive for the open-shell family, and the correct search outcome on it is a negative.** A 100 mm box with a 5 mm chamfer face deleted (loop 58 % of diag again, along a 100 mm edge) also declined every edge. A positive needs a hole under 5 % of the diagonal; I did not build one. Also: **`DetGenerator` never proposes `fill_loop`** even for open sites, and the open site carries no edges to target (the finding names shell H1, no free-edge ids). Neither is fixed. This goes to the owner: before the 30-file smoke, pick injected open-shell defects whose loop is < 5 % of the diagonal, or the arm will measure "ops decline" for the right reason but on the wrong inputs.

## Q1: op bounds, absolute vs scale-relative

Kernel-checked bounds (ops/shells.py, `_bound`): every rung/width must lie in `[1e-6 x diag, min(0.1 mm, 0.001 x diag)]`. Schema maxima are the absolute 0.1; the real bound is scale-relative and only the kernel knows the diagonal.
- **Scale-relative (the bounds):** sew `tol_ladder` rungs, remove_small `max_width_mm`, fill_loop span (5 % of diag).
- **Absolute (the generator's constants, `search.py:352-356`):** `_SEW_LADDER = [1e-4, 1e-3, 1e-2]`, `_SLIVER_WIDTHS_MM = (0.001, 0.01, 0.05)`, `_SPLIT_ANGLES`/`_PROXIMITY_RATIOS` (dimensionless, fine); and in `stages.py:82` the staged ladder `[0.001, 0.01, 0.05]` and `small_width_mm 0.05`.
- Consequence: the sew ladder is accepted whole only for **10 mm <= diag <= 100 mm** (floor 1e-6*diag <= 1e-4; bound 0.001*diag >= 1e-2); any rung off makes the WHOLE candidate fail (`stage: failed`, a ValueError from the kernel). Measured: diag 173 mm gives `rungs [0.0001] must lie in [0.000173, 0.1]`. `remove_small` 0.05 needs diag >= 50 mm; the staged 0.05 rung needs diag >= 50 mm. On a corpus of watch parts to assemblies this silently disables sew/remove_small outside those bands. **The bounds are right (they are the fidelity bound); the generator constants are wrong.** NOT FIXED: the det generator has no diagonal (`SearchState` carries none; `quantum` is not invertible to it). Recipe: carry `diag_mm` into the generator (from `ws_diagonal`, already called by `h_mm_for`; `h = diag/10`) and emit rungs as fractions of diag clamped into `[floor, bound]`. Needs a corpus-diagonal histogram first (stratification).

## What I did not fix
- scale-relative ladders/widths (above); `fill_loop` not in the det sweep; free edges not located; `H_MM = 1.0` (Task 35); mesher-only whole-part failures still map to `unlocated` regardless of `error_kind` (e.g. `no_volume` -> `open` is in `_MESHER_KINDS` but unused when unplaced); T31's other gaps (arm selection, `__main__` guard, summary fields). A ValueError for out-of-range rungs is classed `failed`, not `declined`/invalid-proposal; left as is.

## Tests and mutations
Added: `tests/unit/cad/repair_v4/test_sites.py::test_hard_finding_that_cannot_be_placed_keeps_its_kind`, `test_search.py::test_det_generator_proposes_for_a_whole_part_open_site`, `tests/native/test_native_repair_v4_open_shell_candidates.py` (real box with a face deleted via ReShape; asserts an `open` site, `verdict_candidates > 0`, a scored candidate, no infra failure; asserts generation and judging, not repair).
- Mutation A (revert the sites.py line): native test FAILS, `test_hard_finding_that_cannot_be_placed_keeps_its_kind` FAILS (exit 1 both). Restored.
- Mutation B (`if kind == "open"` -> `"opened"` in the sweep): `test_det_generator_proposes_for_a_whole_part_open_site` FAILS (exit 1); the native test still PASSES, because `orient_and_close` from the `invalid` site still reaches a verdict (so the native test guards the site fix, the unit test guards the sweep). Restored; tree clean.

## Id diff, `tests/unit/cad/repair_v4`, validation image, clones with no `v4-` in the path
head: 544 passed, 0 failed (exit 0). base `33c57842`: 542 passed, 0 failed (exit 0). (The two known `main` failures did not occur in this directory.) `comm -23` head-only: EMPTY. `comm -13` base-only: EMPTY. Both FAILED/ERROR lists are empty.

## Commands (real exit codes)
- diagnosis drivers `/tmp/t36/{drive,small,fill}.py` in native image, absolute `PYTHONPATH=/w/src`: exit 0 each (drive.py needs a `__main__` guard: the known spawn finding; without it EOFError, exit 1).
- new unit tests, validation image: exit 0 (2 passed). New native test, native image: exit 0 (1 passed, 23 s).
- Mutation A native 1 / unit 1; Mutation B native 0 / unit 1; baseline native 0 / unit 0 (test_sites+test_search 64 passed).
- id diff head / base: exit 0 / exit 0, written to files, no pipe on pytest's code.
