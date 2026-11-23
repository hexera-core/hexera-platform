# Task 32 - Task 24 (harness rows) x Task 22c (arm dispatch): verification and completion of the merge

Head at report time: see `git log -1` on `sdd/task-32` (the report commit sits on top of `c922117e`).
Base compared against: `2d30adc4`. The previous attempt's merge commit was `ddf73736`.

## Verdict on `ddf73736`: the code resolution was real, the merge was NOT complete

The six hunks were genuinely resolved (see below) - no conflict markers, one dispatch, one row writer. But the merge
commit's title was ahead of its tests: **14 tests failed on the merge that passed on neither side's own tip**, and one
of 24's guards had been silently dropped. Id diff of the merge commit against base: 14 new failures
(`test_v4_arms_launch` x2, `test_v4_harness_rows` x12). Causes, all fixed in `2394ca84`:

| Cause | What it was |
|---|---|
| **Lost guard** | 24's `reprobe_mod.check_sources(split)` ("Fusion 360 is in no split") disappeared when 22c's `split_entries` replaced 24's reading. A split naming F360 was still refused, but only by accident ("pool does not carry" - because `split_allowed` had already dropped them). Restored in `split_entries`. Mutation (replace with `pass`) breaks `test_fusion_360_in_a_split_is_refused_before_any_file_runs`. |
| Harness + search arm without `--h-mm` | merged `run_arms` requires `--h-mm` for search arms, 24's tests never passed it. Tests now pass it. The guard in `run_arms` is the ONLY one on the `--mode harness` path (the parser's arm resolution is skipped there) and nothing asserted it: mutation survived. Added `test_a_harness_shard_of_a_search_wave_without_an_element_size_is_refused`; mutation now breaks it. |
| Injected runner still probed the model route | `run_arms` called `_prepare_model_route` (needs OPENAI_API_KEY) even when a per-file `runner` replaced the dispatch. Now only when `runner is None`. Production never injects a runner. |
| Launcher: `--wave` alone | 22c's launcher requires `--arms --split-uri --h-mm` with `--wave`; 24's tests launched with `--wave` only. The merged contract (22c's, stricter) stands; tests carry the flags. |
| `vm_startup.sh` fails closed without a plan | 22c's startup tests had no `v4-plan-provenance`. Added to their meta; new test that `""` and `UNCLAIMED-dry-run` make the startup refuse (rc 1, no `docker run`, a FAILED object). |
| Stale tests | `test_the_startup_script_runs_harness_with_the_plan_only_for_an_arm_wave` asserted `MODE=harness`, which the merge correctly removed: rewritten to assert the merged shape (`--mode arms ... --h-mm`, `--plan-provenance`, fail-closed `case`). `test_no_arm_runner_refuses...` asserted `ArmDispatchMissing`, which no longer exists because the dispatch does: rewritten to assert that with no runner the harness calls `arms.run_file` (one code path). |
| Dry-run second held-out | The launcher's own count and `plan_wave` now read the same key, so the launcher's message ("held-out wave was already launched as ...") wins. Test expectation updated. |

## The six conflict hunks

1. `run_arm.py` header/imports/`ARM_RUNNER`: both kept; `ARM_RUNNER` is now "replaces `arms.run_file` in `run_arms`", None in production.
2. `run_arm.py` `run_arms`: 22c's dispatch with 24's row writer: rows stream to `rows.partial.jsonl`, then ONLY `write_arm_rows` writes `rows.jsonl`/`rows.json`/`EXCLUDED.json`, after `report.parse_arm_rows` has accepted every row; a refusal is exit 4 with no rows file. `--purpose tuning` on held-out refused; split/F360 refusals (now incl. the restored `check_sources`) precede the model-route probe so a bad split costs nothing.
3. `run_arm.py` `build_parser`: 22c's `--arms/--wave/--split-uri/--h-mm/--model-secret` + 24's `--plan-provenance`, `--purpose`, `--split-file`.
4. `run_arm.py` `main`: ledger built (exit 5 on lost log), plan loaded, `adopt_plan` only for `--mode harness`, `plan_provenance_problems` raised BEFORE the plan is stamped (mutation: remove the raise -> `test_a_plan_for_another_wave_is_not_stamped_on_this_shard` fails), harness -> `run_harness` -> `run_arms` (mutation: `return 0` -> 9 tests fail), exit 6 for NoModelKey/NoUsableRoute/ModelArmWithoutLedger.
5. `launch_fleet.py`: 22c's arms metadata + 24's `plan_metadata` (JSON plan, or `UNCLAIMED-dry-run`), `--wave` declared once (24's choices=WAVES), `plan` default, wave type from the wave definition, claim through `plan_wave` in `v4_gate_problems`, metadata re-rendered after the claim.
6. `vm_startup.sh`: 22c's arms mode + `--h-mm` + split mount, 24's `PLAN_PROV` with fail-closed `case`, and `--plan-provenance` on the SAME `--mode arms` command.

## `prov["ledger"]` fix: SURVIVED
`run_arm.py` `main`: `prov["ledger"] = ledger.provenance` (the string `"shard"`) and `prov["ledger_detail"] = {...}`. Covered by `test_plan_provenance_is_copied_into_the_shard_and_the_report_can_verify_the_slot` (`prov["ledger"] == "shard"`, plus the report loading the shard). Mutation (`ledger` -> a dict) breaks exactly that test.

## The three open items

1. **`--h-mm`.** Fixed in `vm_startup.sh`: `--h-mm "${HMM}"` is on the arms command (metadata `v4-h-mm`). The resolution chose the **wave-level flag**; `arms.RealEngine.judge(h_mm)` uses that one number for every file. 22b's per-part derivation (`search.h_mm_for` / `default_judge_for`, `1000 * base_size(diag/1000)` via `ws_diagonal`) exists and is NOT used by the dispatch. **Open decision for the controller:** one element size for a whole split of parts of very different size vs the per-file derivation. I did not change it.
2. **Two slot ledgers: genuinely unified.** `wave_limits.HELD_OUT_KEY = "slot/held_out/1"`, `checkpoint_key(n) = "slot/checkpoint/n"` - the keys `evalspec.slot_keys`/`claim_wave_slot` use and the report reads. The launcher's `check_wave_count` and `plan_wave` therefore claim/see the same marker; no `_waves/...` reference remains in the tree. Added `test_there_is_one_slot_ledger_...` (keys equal `ev.slot_keys`) and `test_a_slot_the_launcher_claimed_is_the_slot_plan_wave_returns`; mutating either key back to `_waves/...` breaks them. **Caveat:** a bucket that already holds `_waves/...` markers from an earlier real launch would not be seen (no migration). I found no evidence any real held-out wave ran; confirm before the held-out launch.
3. **v3 arm rows.** `heldout` lists `v3_meshability_pass_ai`, dispatchable nowhere in v4; `report.load_wave` raises `ReportRefused: ... have no row (... v3_meshability_pass_ai)`. Demonstrated in the held-out test (unmerged shard refused; with v3 rows appended to `rows.json` it loads, `slot_verified`). **The merge step must:** run v3_eval's own run_arm over the same split's files, convert each result to an `ArmRow` dict for arm `v3_meshability_pass_ai` (full verdict fields or `inconclusive: true`), and append them to the collected shard's `rows.json` (or write a shard dir) before `load_wave`. Not built.

## Closing check against the REAL dispatch
Runner: a 10-line factory over `arms.run_file` + `RealEngine` (not committed; `/tmp/t32f/real_dispatch.py`), passed via `--runner`. Image `meshpipeline-mesh:native`, absolute `PYTHONPATH`.
- **The committed check, unmodified, FAILS against the real dispatch** (exit 1) at its 7th assertion: `the file whose checker had no gmsh reads inconclusive: true (no row)`. The real dispatch returns an `Excluded` for an unjudgeable input (`checker_unavailable`), written to `EXCLUDED.json` with NO row, whereas the built-in `input_only_runner` writes an `inconclusive: true` row. `report.load_wave` (report.py:331) turns an excluded file into an inconclusive row for every arm, so the report-level outcome is identical.
- With the row-level assertion changed to "no row, listed in EXCLUDED.json" (scratch copy `/tmp/t32f/check_real.py`, not committed) the whole check PASSES against the real dispatch (exit 0, on `8a3ad976`; the later commits only add tests): dead file carries no verdict fields; **clean rate 9/10** (not 10/11, not 9/11), `inconclusive 1, conclusive 10, measured`; `input_only`'s `in_clean` equals the split's own verdict on every judged file; the naive conclusive-without-verdict row is refused.
- **Decision for the controller:** which contract is right for an unjudgeable *input* - an `inconclusive` row (check's wording, search arms' behaviour) or an exclusion (what `arms.run_file` does for input_only/staged). Both reach the same report numbers today. I did not change either, and did not edit the committed check.

## Id diff (docker `meshpipeline-validation:local`, which has git; `meshpipeline-mesh:native` has no git and cannot collect these tests)
Paths: `tests/unit/dev tests/unit/cad/repair_v4 tests/unit/settings`, clones with no `v4-` in the path, `PYTHONPATH=/w/src`.
- base `2d30adc4`: 8 failed, 2068 passed. Merge commit `ddf73736`: 22 failed. Final `8a3ad976`: 8 failed, 2100 passed, 2 skipped.
- `comm -23 final base` (final-only): **empty**. `comm -13 final base` (base-only): **empty**. The 8 are exactly the known two (`test_the_settings_tier_passes_with_every_required_setting_hostile`, `test_no_machine_specific_path_reaches_the_template`) plus the six configuration-authority failures from `cache.py:209` (Task 33).
- v3 goldens: `pytest tests/unit/dev/test_v4_fleet_launcher.py -k v3` -> 7 passed, exit 0 (includes `GOLDEN_V3_CREATE`/`GOLDEN_V3_STARTUP`). They also pass inside the full run.
- ruff on the three changed py files: exit 0. mypy on `run_arm.py`: "Success: no issues found".

## Mutations (each run, restored, what broke)
| Mutation | Broke |
|---|---|
| `prov["ledger"]` as a dict | `test_plan_provenance_is_copied_into_the_shard_and_the_report_can_verify_the_slot` |
| drop `check_sources` in `split_entries` | `test_fusion_360_in_a_split_is_refused_before_any_file_runs` |
| drop the plan mismatch raise | `test_a_plan_for_another_wave_is_not_stamped_on_this_shard` |
| `HELD_OUT_KEY` back to `_waves/held_out` | `test_a_dry_run_of_a_second_heldout_wave...`, `test_there_is_one_slot_ledger_...` |
| `checkpoint_key` back to `_waves/checkpoint-n` | `test_there_is_one_slot_ledger_...` |
| `planarg=()` in vm_startup | startup-shape test (harness_rows) and `test_startup_runs_run_arm_in_arms_mode_with_the_split_mounted` |
| `run_harness` -> `return 0` | 9 harness tests |
| `parse_arm_rows` -> `[]` in `write_arm_rows` | `test_a_search_that_measured_nothing_cannot_be_written_as_a_conclusive_row` |
| tuning-purpose guard -> `if False` | `test_a_tuning_purpose_cannot_run_the_heldout_split` |
| `run_arms` h-mm guard -> `if False` | survived at first; new test added; now breaks `test_a_harness_shard_of_a_search_wave_without_an_element_size_is_refused` |

No model call, no GCP call, no VM, no spend; no `pkill`. `MIN_PAIRED_FRACTION` untouched.
