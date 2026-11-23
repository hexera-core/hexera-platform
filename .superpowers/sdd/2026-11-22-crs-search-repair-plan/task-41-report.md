# Task 41 report: unbounded inputs get a defined verdict in layer 1

Branch `sdd/task-41`, based on `3a1ca42a`. Code head is `bf4b8db5`. The report commit follows it, so use `git log -1` on the branch for the final SHA. Nothing was merged or pushed. No model was called, no GCP call was made, and no VM was launched.

## What changed

- `src/meshpipeline/cad/repair_v4/units.py` defines `UNBOUNDED_DIAGONAL_MM = 1e9`, with the justification written next to it.
- `verdict/valid_kernel.py` adds `_unbounded_finding`. `check_shape` runs it first and returns early with a single hard finding, `unbounded_geometry`. BRepCheck, the shell/manifold checks, the self-intersection check and the Booleans are never run on an infinite shape.
- `verdict/valid.py` adds `unbounded_geometry` to `HARD_CODES`, `ValidReport.unbounded`, and a skip of the round trip. A units mismatch is therefore not also reported.
- `verdict/assemble.py` adds `Verdict.out_of_scope`. `_assemble` returns `Verdict(valid, [], None, clean=False, inconclusive=False)` before the cache, the panel or layer 3 are touched. Nothing meshes it.
- `search.py` (`DetGenerator`) and `stages.py` (`_Steps.diagonal`) now note "unbounded input (diagonal 3.46e+100 mm exceeds 1e+09 mm, layer 1 reports unbounded_geometry): no diagonal-relative op applies". They no longer say "empty window" or "diagonal unknown". The diagonal computation (`ws_diagonal`, `diagonal_mm`) and every kernel bound are untouched.
- `devtools/validation/v4_eval/reprobe.py` and `arms.py` carry the stratum through (see Scoring).

## Threshold

`UNBOUNDED_DIAGONAL_MM = 1e9` mm (1,000 km) on the bounding-box diagonal. A box with an open side also counts, and NaN counts. The BREP is always in mm.

- **Low end.** A 200 m cube has a diagonal of 3.5e5 mm, and a 100 km beam has 1e8 mm. The corpus p95 is 2.2e4 mm. The limit is about 3,000 times above a 200 m cube's diagonal and about 10 times above the 100 km beam. It sits above any single part or assembly a CAD file holds.
- **High end.** OCCT writes infinite geometry into a box as +-Precision::Infinite, which is 1e100 per side. The diagonal is therefore 3.46e100 in 3D, matching the corpus's 3.5e100. The limit is 91 orders of magnitude below that.
- **Measured both ways.** Real kernel runs on both sides are listed below. A mutation that sets the limit to 1e5 catches the 200 m cube and the 100 km beam, and the tests fail.

## How it scores

It is a third category: **measured, not clean, not a failure, not inconclusive**.

| field | value | why |
|---|---|---|
| `Verdict.clean` | False | nothing certified it |
| `Verdict.inconclusive` | False | the checker did measure it, so the unmeasured rate that Task 21 reads does not move |
| `Verdict.counts_for_scoreboard` | True | it is measured, the same as `no_solids` |
| `Verdict.out_of_scope` | True | the new, separate signal |
| panel | `[]` | no mesher was asked |
| faithful | None | layer 3 not run |

Downstream, it follows the `no_solids` precedent.

- **Reprobe.**
  - It produces a counted `ProbeRow` with `first_failing_layer="valid"` and `family="unbounded_geometry"`.
  - `summarise` records `out_of_scope: true`.
  - It is not added to the `excluded` list. That list means "not measured", and `collect.py` refuses a pool with more than 2% excluded. About 3% unbounded files would trip that limit.
  - The empty-panel exclusion no longer applies to out-of-scope rows.
- **Splits.** `full_tier_entries` does not give them the full panel. `build_splits` keeps them out of the failing set (dev and heldout) and out of clean. They are tallied alone as `Splits.excluded_unbounded` and written to the split meta as `excluded_unbounded`, next to `excluded_no_solids`.
- **Arms.** `arms._usable` labels one that slipped through as `out_of_scope: ...`, not "inconclusive". The splits keep them out, so a wave should never reach this.

**What this does to an arm's clean rate: nothing.** An unbounded file never enters dev, heldout or clean. It is therefore never in any arm's denominator or numerator. It is not a failure that repair "attempted and lost", and it adds nothing to the inconclusive count behind Task 21's 10% withholding rule. The rate is over the judgeable files, and the unbounded count is reported separately, so its size is visible: roughly 3% of cleared files, about 300.

If it ever did reach `run_file` (it should not), it would be excluded under the reason prefix `out_of_scope`. `collect.py` counts that toward `excluded_fraction` but lists it separately in `excluded_by_reason`.

## Real unbounded cases (real kernel, `meshpipeline-mesh:native`)

1. **A solid plus an infinite edge.** A 10 mm box and an unbounded `Geom_Line` edge in one compound, written to STEP and read back through the worker pool.
   - Before the change (base): `check_valid` returned `ok=True`, no findings, 1 solid, round trip OK. A 1e100 part would have gone on to the panel.
   - After the change: `hard=[unbounded_geometry]` only, with `diagonal_mm=3.46e100` and all six sides open.
   - An infinite plane face does not survive STEP. The writer drops it, so `box + infinite planes` re-reads as the bare box and `planes.step` as no solids. The infinite line does survive, with a bbox of exactly +-1e100 per side.
2. **An infinite half-space solid** (`BRepPrimAPI_MakeHalfSpace`), run through the kernel `check_valid` as a BREP. It is classified `unbounded_geometry`, with open sides.
3. **A real corpus file.** `mesh-fail-corpus-for-areen/abc/00502200_5895d152ed2c0c0fe9ef029c_step_093.step`.
   - Layer 1 returns `[unbounded_geometry]` with diagonal 3.46e100, 178 solids, and rest skipped.
   - Cause: 2 faces lie on untrimmed `Geom_CylindricalSurface` (infinite in v). They sit in 2 stray shells outside any solid.
   - Local scan of that corpus: 1 of 54 files exceeds 1e9 mm. That is the same 3.46e100, and it was the only one.

## Are Task 40's nine real?

Partly answered, cheaply. The Task 40 sample itself is not available locally. In the one local file I could inspect, the 1e100 box is real unbounded CAD, not a mis-read. It is a pair of untrimmed cylinder faces left in a 178-solid file. OCCT is right about the geometry, and no diagonal-relative op can serve such a file, because the extent is defined by geometry that has no extent. The corpus-composition conclusion is that at least some of the 3% are genuinely unbounded. Whether all nine are is **not established**. n=1 locally, and it comes from the failing corpus, not the cleared one. I did not read the nine cleared files.

## Negative controls (real kernel)

Boxes written to STEP and run through `check_valid` give `unbounded=False` and no `unbounded_geometry` finding for:
- a 200 m cube (200,000 mm per side)
- a 100 km x 3 m x 3 m beam
- a 4.4 x 1 x 1 mm box (the corpus minimum)

## Mutations (each restored afterwards)

| mutation | broke |
|---|---|
| `_unbounded_finding` always returns None | native: infinite-curve, half-space, verdict-out-of-scope (3) |
| limit 1e5 | native: infinite-curve (detail check), 200 m cube, 100 km beam (3) |
| limit 1e101 and the open-sides test removed | native: the same 3 as the first row |
| `_assemble` short-circuit disabled | `test_an_unbounded_input_is_measured_out_of_scope_and_never_meshed` |
| `check_valid` early return disabled | `test_an_unbounded_finding_stops_layer_1_before_the_round_trip` |
| `build_splits` stops excluding unbounded | `test_unbounded_inputs_are_counted_but_never_a_failure_and_are_tallied_alone` |
| `full_tier_entries` stops skipping unbounded | `test_unbounded_rows_do_not_get_the_full_panel` |
| the sweep's unbounded reason removed | `test_an_unbounded_part_is_noted_as_unbounded_not_as_an_empty_window` |
| `stages._Steps.diagonal` unbounded note removed | `test_the_staged_shells_note_an_unbounded_part_as_unbounded` |
| `reprobe` empty-panel guard removed | `test_an_unbounded_input_is_a_counted_row_with_its_own_family_not_an_exclusion` |

## Failure-id diff (`tests/unit/cad/repair_v4` + `tests/unit/dev`, validation image)

The branch run is at head `bf4b8db5`. The base run is a clean clone of `3a1ca42a`. Both ran with `-rfE`; FAILED/ERROR lines were extracted, `sort`ed, then compared with `comm`.

- Branch: 1838 passed, 1 failed, 2 skipped. Base: 1828 passed, 1 failed, 2 skipped.
- Both failure sets are the same single id: `test_onboarding_contract.py::test_the_settings_tier_passes_with_every_required_setting_hostile`. The other known failure, `test_no_machine_specific_path_reaches_the_template`, is a nested subprocess result inside that one test, not a separate id.
- `comm -23` (branch only): **empty**. `comm -13` (base only): **empty**.
- The diff proves little: the only failure is one known on both sides, so it shows only that I added no new failures. It is not evidence that the change is correct.

A first branch run at `f487f7b7` did show one new failure, `test_mypy_ratchet_verdict` (a `float | None` narrowing error I introduced in `DetGenerator._sweep`). I fixed that. The ratchet now reports "27 mypy error(s), all in the checked-in baseline (0 new)". ruff is clean on the changed trees.

## Commands and exit codes

All Docker runs used a `git clone --no-hardlinks` under `/tmp` (no `v4-` in the path), `PYTHONPATH=/w/src` (absolute), and `git config --global --add safe.directory /w`. Exit codes were redirected to files and echoed separately, never read through a pipe.

| command | exit |
|---|---|
| native: `tests/native/test_native_repair_v4_valid.py` (34 passed) at the first test commit | 0 |
| native: `tests/native/test_native_repair_v4_valid.py tests/unit/cad/repair_v4` at `bf4b8db5` (619 passed) | 0 |
| validation: `tests/unit/cad/repair_v4 tests/unit/dev` at `bf4b8db5` (1838 passed, 1 failed) | 1 (the known failure) |
| validation: same paths on base `3a1ca42a` (1828 passed, 1 failed) | 1 (the known failure) |
| `mypy_ratchet.py` | 0 |
| `ruff check` on the changed trees | 0 |
| mutation runs, 10 of them | 1 each when a test broke (see the table) |
| unmutated reprobe control run (74 passed) | 0 |
| real-file scan of the local corpus and the probe of the one unbounded file | 0 |
