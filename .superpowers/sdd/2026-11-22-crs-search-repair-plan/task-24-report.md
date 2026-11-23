# Task 24 report: the eval harness side

Branch `sdd/task-24`, base `3f291efb`. Code head `e9e74481`; the report is the commit on top.
Nothing was pushed or merged; no model, GCP call or VM was used.

## What the row contract imports versus defines

- `ArmRow` (report.py) still DEFINES the row's fields (it is the consumer's schema). The rule "was this file measured" is no
  longer defined there. It is `src/meshpipeline/cad/repair_v4/measurement.py` (`no_verdict_failure`, `file_unmeasured`,
  `NO_VERDICT`), imported by `search.search` (which writes `infrastructure_failed`/`reason`), by `SearchResult.unmeasured`,
  and by `ArmRow.derived_inconclusive` (lazy import; the module is stdlib-only so the host report loads no kernel).
- That file, the `search.py` hunk and the `derived_inconclusive` hunk are BYTE-IDENTICAL to Task 22c's commit `efbe4ef2`
  (22c arrived at the same design; copied rather than re-invented so the merge is a no-op). `cmp` confirmed the file.
- `report.SEARCH_ARMS` (the second hard-coded list) is pinned to `search.ARMS` by a test; it is not imported because `search`
  loads the engine.
- `plan_wave` provenance: `WavePlan.provenance_argv()` (new, evalspec.py) -> `run_arm --plan-provenance` -> `PROVENANCE.json`.

## What was built (scope items 1-3)

1. `run_arm --mode harness` with a plan writes the arm rows: for every file of the shard's slice of the plan's split, for every
   arm of the plan, it calls `ARM_RUNNER(entry, arm, source)` (Task 22c's dispatch, faked here). `write_arm_rows` passes every
   row through `report.parse_arm_rows` BEFORE writing `rows.jsonl`/`rows.json`/`EXCLUDED.json`, so a row the report would
   refuse (a `no_verdict` search written `inconclusive: false`) fails on the shard, exit 4, with no rows file left behind.
   Unreadable / timed-out / raising files are EXCLUDED, never rows. No runner -> `ArmDispatchMissing` (refuses, writes nothing).
   `--mode harness` WITHOUT a plan is still repair_corpus's own run (an existing test pins it), and writes no ArmRows.
2. Launcher: `launch_fleet.py --profile v4 --wave <name> --wave-id <id>`. Type (hence cap) comes from the `WaveDef`; a
   contradicting `--wave-type` is refused. A dry run reads the slot (`evalspec.wave_slot_problem`, read-only); the live claim
   calls `evalspec.plan_wave` inside the create gate, then re-renders every VM's metadata in place with
   `v4-plan-provenance=<plan.provenance()>`. `vm_startup.sh` runs `--mode harness --plan-provenance` when that metadata exists
   and `--mode reprobe` exactly as before otherwise.
3. Provenance: the shard copies the plan (`wave, wave_id, wave_type, split, arms, n_shards, wave_cap_usd, shard_cap_usd, slot`)
   after checking it against its own flags (`plan_provenance_problems`: wave_id, type, N, wave, split, cap vs the ledger table,
   slot vs the type's own keys). A plan for another wave is refused, not stamped. `arms`/`split` are not overwritten by the
   harness (the report compares them); the dispatched arms are `arms_run`.
   **A latent bug found and fixed on the way:** `run_arm` wrote `prov["ledger"]` as a dict, but `load_wave` requires the string
   `"shard"`, so EVERY real model-wave shard would have been refused ("ran without a shard ledger"). Now `ledger = "shard"`
   and the dict is `ledger_detail`.
- Held-out hook: `--purpose {report,tuning}` (default report); `tuning` on the held-out plan calls
  `evalspec.refuse_heldout_for_tuning`. Fusion 360: `check_sources(split)` before any file runs; a split naming it exits 4.

## The six carry-ins, each with its test (all in tests/unit/dev/test_v4_report.py)

| # | change | test |
|---|---|---|
| 1 | `primary_artifact = heldout and slot_verified`; banner says "NOT the primary artifact" otherwise; `heldout_split` key added | `test_primary_artifact_needs_a_slot_the_marker_store_confirmed`, `test_a_dev_report_is_never_the_primary_artifact_even_with_a_verified_slot`; the old frozen-commit test asserted the cheap banner and was corrected |
| 2 | indistinguishable warning ranges over MEASURED arms only | `test_the_indistinguishable_warning_never_names_an_arm_that_is_not_measured` (sibling: two measured identical arms ARE flagged) |
| 3 | per-kind `clean` is `None` when the kind is over 10% inconclusive, on a measured arm | `test_per_kind_cells_on_a_measured_arm_are_withheld_...` (arm 5% inconclusive, kind 20%; sibling kinds keep figures) |
| 4 | none (code already right) | `test_clean_gain_and_paired_clean_in_are_asserted_numerically_on_a_split_with_clean_inputs` (every 5th input clean, one clean input dropped from one arm so paired `clean_in` is 7 not 8; expected values computed from the fixture's own `_fixed`, not the report) |
| 5 | none | `test_a_search_row_whose_start_is_clean_is_measured_even_with_inconclusive_candidates_and_no_verdict` (+ the same fields on an unclean start are refused) |
| 6 | shared definition (above); `SEARCH_ARMS` pinned | `test_the_row_carries_exactly_the_fields_the_search_driver_defines`, `test_a_real_search_result_and_the_report_row_agree_on_whether_a_file_was_measured` (grid over a real `SearchResult`) |
| + | min-paired-n warning, `MIN_PAIRED_FRACTION = 0.80` (PROVISIONAL, owner's to set) | `test_four_measured_arms_that_each_drop_different_files_warn_that_the_paired_set_shrank` (28 of 40; sibling 37 of 40 silent) |

Harness/launcher tests: `tests/unit/dev/test_v4_harness_rows.py` (20 tests).

## Mutations (each applied to a scratch copy, restored by re-sync; tests = test_v4_report.py + test_v4_harness_rows.py)

| mutation | broke |
|---|---|
| primary_artifact without `slot_verified` | `test_heldout_report_requires_frozen_commit`, `test_primary_artifact_needs_a_slot_the_marker_store_confirmed` |
| indistinguishable over all arms | `test_the_indistinguishable_warning_never_names_an_arm_that_is_not_measured` |
| per-kind `clean` shown when kind is over the limit | `test_per_kind_cells_on_a_measured_arm_are_withheld_...` |
| paired gain `clean + cin` / drop `cin` / arm gain `clean_out + clean_in` | `test_clean_gain_and_paired_clean_in_are_asserted_numerically_...` (each of the three) |
| drop `and not out_clean` from `file_unmeasured` | `test_a_search_row_whose_start_is_clean_is_measured_...` |
| remove min-paired warning | `test_four_measured_arms_that_each_drop_different_files_warn_...` |
| report restates the rule (without the third clause) | `test_a_real_search_result_and_the_report_row_agree_...`, `test_a_search_row_whose_start_is_clean_...`, `test_a_search_that_measured_nothing_cannot_be_a_conclusive_failure[over2]`, `test_the_row_carries_exactly_the_fields_...` |
| harness does not copy the plan | `test_a_shard_without_the_plan_is_what_the_heldout_report_refuses`, `test_plan_provenance_is_copied_...`, `test_the_harness_writes_rows_the_report_loads_and_counts` |
| harness writes rows unvalidated | `test_a_search_that_measured_nothing_cannot_be_written_as_a_conclusive_row` |
| launcher does not call `plan_wave` | `test_a_second_heldout_wave_is_refused_and_creates_no_vm`, `test_the_launcher_claims_the_wave_through_plan_wave_and_hands_every_vm_the_plan` |
| held-out tuning hook disabled | `test_a_tuning_purpose_cannot_run_the_heldout_split` |
| `ledger` back to a dict | `test_plan_provenance_is_copied_into_the_shard_and_the_report_can_verify_the_slot` |
| `unmeasured_rows` folded to 0 | `test_an_inconclusive_row_and_an_excluded_file_are_written_as_such_never_as_failures` |
| closing check's runner writes `inconclusive: false` for the dead file | the check FAILS at "shard 2/2 exited 0" (the shard refuses the row) |

Fixture weighing: the "arms differ" fixtures are the existing non-nested `_fixed` sets; the new numeric test computes its
expectations from them independently and asserts the gain differs from `clean_out` and `clean_out + clean_in`; the
indistinguishable test has a sibling that must warn, so the filter cannot pass by suppressing everything.

## The closing check (RAN, passed)

`devtools/validation/v4_eval/check_infra_not_verdict.py`, in `meshpipeline-mesh:native` (kernel + gmsh + Netgen), PYTHONPATH
absolute, no model/GCP. 11 real STEP files (9 boxes, 1 real defective ABC part, 1 box steered by a header comment into the
gmsh-less shard). Control: the real full-tier verdict (gmsh present) = the split's own verdict (10 clean, 1 unclean). Wave:
`run_arm --mode harness` shard 1 normal; shard 2 with a shadow `gmsh.py` that raises ImportError in the worker. Result
(21 assertions): the dead file's row is `inconclusive: true`, carries no verdict fields, and its cause was
`CheckerUnavailable: gmsh_prod: mesh_gmsh: ... ImportError` (missing gmsh, not something else); the 10 live rows all have
verdicts and `in_clean` equals the split's on every one; the report counts 1 inconclusive / 10 conclusive, clean_in =
clean_out = 9, clean rate 9/10 (not 10/11, not 9/11), the dead file really is a clean one so counting it would have changed
the rate; a naive conclusive row with no verdict is refused by the row contract. It ran against the head code (`e9e74481`).
It uses a built-in input_only runner (a real `verdict(pool, step, step)`), NOT 22c's dispatch. To run it against the real
dispatch: adapt `arms.run_file` onto `run_arm.ARM_RUNNER` as a factory `f(pool_fn) -> runner` and pass `--runner pkg.mod:f`.

## Failure-id diff (clean `git clone --no-hardlinks` to /tmp/t24base and /tmp/t24head, absolute PYTHONPATH, `tests/unit/dev` + `tests/unit/cad/repair_v4`)

- base `3f291efb`: 1 failed, 1618 passed, 2 skipped, exit 1. Branch `e9e74481`: 1 failed, 1647 passed, 2 skipped, exit 1.
- `comm -23 base.ids head.ids` (fails on base, not on branch): EMPTY. `comm -13 base.ids head.ids` (fails on branch, not on base): EMPTY.
- The single shared failure is `tests/unit/dev/test_onboarding_contract.py::test_the_settings_tier_passes_with_every_required_setting_hostile`
  (a home-directory path in a rendered env template; fails identically on base, untouched here).
- An earlier branch run found two REAL regressions from my change, now fixed and in the final diff: `args.plan` (launcher) and
  `args.split_file` (run_arm) are read by code but are not parser flags, tripping
  `test_the_launcher_parser_defines_every_arg_the_launcher_reads` and `test_run_arm_parser_carries_every_flag_run_reprobe_reads`.

## Other commands and exit codes

- `docker run ... pytest tests/unit/dev/test_v4_harness_rows.py tests/unit/dev/test_v4_report.py`: 0 (after fixes).
- 7 related files (preflight, reprobe, harness_rows, report, wave_controls, fleet_launcher, evalspec): 382 passed, rc 0.
- `ruff check` on all changed/new files: clean after one `--fix` (import order) and one C401 edit in the check script.
- `mypy` on `search.py` + `measurement.py`: Success (0 errors); on the four changed devtools files: Success.
- All gate runs exit codes were read from files (`echo $?` into a `.rc`), never through a pipe.

## Merge notes and open items for the controller

- 22c overlap: `measurement.py`, the `search.py` hunk and the report `derived_inconclusive` hunk are identical to 22c's (clean
  merge). `run_arm.py` will conflict textually in `build_parser` and `main` (22c adds `--mode arms`/`--arm(s)`/`--wave`/
  `--split-file`; I add `--plan-provenance`/`--purpose` and the ledger-string fix + plan copy). `evalspec.py` is adjacent
  (22c edits `shard_argv`, I add `provenance_argv` after `provenance`). Resolution to choose: 22c's `run_arms` already writes
  rows; it should call `write_arm_rows` (row contract before write) and copy the plan, and `--mode harness` with a plan
  should delegate to it. My `run_harness`/`ARM_RUNNER` then reduces to that adapter. 22c's `shard_argv` and my
  `provenance_argv` should be emitted together (VM startup currently passes only `--plan-provenance`; it does not pass `--arms`
  or `--h-mm`, which 22c's arm mode needs; that is the remaining startup wiring).
- Two slot ledgers now coexist: the launcher's `_waves/held_out|checkpoint-N` (wave_limits) and plan_wave's `slot/...`
  (what the report reads). An arm-wave launch claims both. Not unified here.
- The held-out wave lists `v3_meshability_pass_ai`, which v4's run_arm cannot run (v3 runs it under v3_eval); the report
  requires a row for every (file, arm), so the held-out rows from the two runners still need a merge step that does not exist.
- The v4 VM has no model key (`vm_startup.sh`, `model_secret=None`), so `search_ai` waves cannot run until 22c/the owner decide that.
- `MIN_PAIRED_FRACTION = 0.80` is a provisional judgement, like the existing 10%.
