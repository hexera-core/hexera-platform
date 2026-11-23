# Task 39 report: the injector must not generate defects no op may repair

Branch `sdd/task-39`, base `0b847b4b`. Head = the commit carrying this report (`git rev-parse HEAD`); last code commit `54789b76`.
No model called, no GCP call, no VM. Nothing merged or pushed.

## HEADLINE FINDING (read first)
**The fixable defect is fixable, but NO arm of the search finds the fix.** The new end-to-end tests
`test_a_fixable_injected_case_ends_clean[search_det]` and `[search_ai]` are RED, on purpose and for a real reason:
the search stops `exhausted` after proposing only `sew`, `make_volume`, `orient_and_close`. `fill_loop` is never proposed
(DetGenerator has no `fill_loop` row; the open site carries no edges; this is the Task 36 gap, unfixed, not mine).
Also the repair is TWO ops: `fill_loop` leaves a closed shell, and `make_volume` is needed to make it a solid. I kept the
tests red rather than weaken them. The T2 smoke would measure "search never proposed the fix" until that is wired.
Task 40's scale-relative sew does not change this: sew cannot invent a face.

Second finding: span <= 5% is NECESSARY, NOT SUFFICIENT for repairability. A small INCLINED face (2.5 mm corner chamfer
triangle, loop 3.5% of diagonal) is DECLINED by `fill_loop` as `over_bound` (fill deviates 0.83 mm from surroundings,
bound 0.1 mm). Default `face_delete` can still yield such declines, which the report would count as failures. The ruling
said only the 5% span, so I added no second filter. Needs an owner decision.

## Where the 5% bound lives
`src/meshpipeline/cad/repair_v4/limits.py` (stdlib only): `MAX_FILL_SPAN_RATIO = 0.05` and `fill_span_allowed(span, diagonal)`.
- `ops/faces.py` (`fill_loop`) imports both and refuses via `not fill_span_allowed(...)`; the old local constant is deleted.
- `verdict/faithful_kernel.py` (`fill_too_large`) uses them; `FILL_SPAN_FRACTION` is an alias of the shared value (it had been a SECOND typed 0.05).
- `devtools/validation/v4_eval/inject.py` imports the `limits` module and reads it at call time (so a moved constant moves the injector too).
Measured the way the op measures: bbox of the loop's edges over the bbox diagonal of the (damaged) part.

## Injector and stratum
- `face_delete` deletes only faces with `fill_span_allowed`; raises InjectionNotApplicable if none (a bare box has none).
  Labels gain `loop_span_ratio`, `fill_cap_ratio`. Seed->face mapping changed, so inject.py revision hash changes: existing recipes
  will read as revision mismatch on `generate`/verify and need regenerating (pinning of OCCT version / image digest untouched).
- `face_delete_large`: new kind, `KIND_STATUS = "declined_stratum"`, label `expected_outcome = "correctly_declined"`. Opt-in:
  in `ADMITTED_KINDS`, NOT in `DEFAULT_KINDS`. Carried via the existing mechanism (`label["status"]` -> `PoolEntry.kind_status` -> `ProbeRow.kind_status`).
  `inject_pool.check_kind` and `reprobe.check_not_quarantined` admit it as itself only (status must equal the injector's for the kind).
  Excluded from calibration (`_case_family` raises).
- `report.py`: `load_wave` refuses a declined kind whose split status was dropped (would be a silent failure row) and a stratum status on another kind.
  Declined files are removed from files/conclusive/inconclusive counts, clean rate, gain, regressions, layers, per-kind, paired set, marginals, and the
  10% inconclusive test; they appear only under `declined_stratum` (declined / unexpectedly_clean / inconclusive) and a separate text section.
  Their spend and wall still count in the bill. An all-declined split is refused.

## Numbers
Fixable (flush 3x3 mm square in 100x60x40 box, `face_delete` seed 3): loop 3.4412% of diagonal (4.243 mm); `fill_loop(['E21','E22','E23','E24'], continuity G0)`:
planar, fill_to_surroundings 0.0 mm, bound 0.1 mm; then `make_volume` on all 9 faces -> real full judge: valid, faithful, 4/4 panel pass, `clean=True`.
Declined (`face_delete_large`): loop 87.36% of diagonal; `fill_loop` raises OpDeclined `too_large ... refuses beyond 5%` (declined, not failed); label status `declined_stratum`, expected `correctly_declined`.
(Task 36's 57.7% was a bare 10 mm cube; a face with an imprint spans 87% here.)

## Mutations (each run, restored, files unchanged afterwards)
Unit: M1 declined rows folded into arm figures -> 3 tests fail; M2 drop stratum-status refusal -> 1; M3 stratum spend dropped -> 1;
M4a gate admits DEFAULT only -> 1; M4b own-status branch off -> initially SURVIVED, added `test_an_entry_of_any_source_carrying_a_status...`, now 3 fail;
M4c injector-status equality off -> 2; M5 pool ignores ADMITTED -> 1; M5b stratum default draw -> 1.
Native: N1 cap=0.9 -> 4 fail (+fixture errors); N2 injector ignores cap -> 9 fail; N3 injector private literal 0.05 -> 1 fail (drift test);
N4 label drops expected_outcome -> 2; N5 op uses local 0.9 -> 2.
Fixture weighing: the report fixtures give declined rows values that would change every figure if folded (input 2 passes -> output 0, one search_det "repair", all-inconclusive stratum that would push arms past 10%); the fixable repair is proven on the real judge, loop span measured from the FILE not the label.

## Id diff (clean clones with no `v4-` in the path, validation image, absolute PYTHONPATH, `tests/unit/cad/repair_v4 tests/unit/dev`)
head: 1822 passed, 1 failed (rc 1); base 0b847b4b: 1805 passed, 1 failed (rc 1). Failure ids both sides: only
`tests/unit/dev/test_onboarding_contract.py::test_the_settings_tier_passes_with_every_required_setting_hostile` (known).
`comm -23 head base` (head-only): EMPTY. `comm -13` (base-only): EMPTY. The diff proves little: both sides carry only the known failure.

## Native (mesh image, clean clone): inject, inject_pool, fill_cap, end_to_end
63 passed, 3 failed (rc 1): the two RED fixable-ends-clean tests above, and `test_repair_v4_is_callable_from_a_script_without_a_main_guard`
which ALSO fails on base 0b847b4b (1 failed, rc 1; known spawn finding, not mine).
ruff: clean on changed files except pre-existing C416 at end_to_end:~188. mypy on limits.py, faces.py, faithful_kernel.py: success.

## Test changes worth knowing
The e2e `defect_step` was a 10 mm box `face_delete`; that now (correctly) raises NotApplicable, so it is a flush-patch box
(`tests/native/_v4_flush_patch_part.py`, shared helper). `test_native_v4_inject*.py` sources adjusted (the filleted fixture has no small face;
the pool fixture imprints a 1.4 mm flush square). Added `tests/native/test_native_repair_v4_fill_cap.py`.
Not touched: `run_arm.py`, `launch_fleet.py`, `vm_startup.sh`, `search.py`. Out-of-range sew rungs classed FAILED not DECLINED (Task 36 note): not hit here; Task 40 reportedly changed it.
Fusion 360 appears nowhere. Task 40 (`3a1ca42a`) landed after my base; I did not rebase.

## Commands (real exit codes)
unit gate head/base: rc 1 / rc 1 (each 1 known failure). native head: rc 1 (3 failed as above). native base main-guard test: rc 1.
Targeted runs: report/reprobe/inject_pool unit files 126 passed rc 0; native inject+pool+fill_cap 54 passed.
