# Task 35 report: the engine seam gaps Task 31 exposed

Branch `sdd/task-35`, based on `cada9133`. Head commit: see `git log -1` (report commit sits on top of `35ab97e1`).
No model called, no GCP call, no VM. Files of Tasks 32/34 (`run_arm.py`, `launch_fleet.py`, `vm_startup.sh`) NOT touched.

## Task 31's end-to-end test after the fixes (native image, real kernel, real pool, real panel)

Task 31's file was copied unchanged from `sdd/task-31:1efbc43b` into this branch (identical bytes, so the later merge is a no-op). No assertion edited.
Result: **9 passed in total with the engine-entry tests; 3 failed**. Task 31's file alone: 7 passed / 3 failed of 10 (it has 10 tests: 4 + 1 + 4 + 1; the brief's "7 failed / 2 passed" counted 9).

| arm | `repair_v4()` (was) | wall-clock | `arms.run_file` | wall-clock |
|---|---|---|---|---|
| input_only | PASS (was ValueError) | 28.4 s | PASS | 0.51 s |
| staged | PASS (was ValueError) | 25.6 s, delivered | PASS | 0.82 s |
| search_det | PASS (ran, failed assertion 3) | 24.8 s | FAIL: 0 verdict candidates | 0.60 s |
| search_ai | PASS (was TypeError) | 24.8 s, 4 requests on the wire | FAIL: 0 verdict candidates | 0.64 s |
| h_mm test (search_det) | PASS | 30 s | | |
| no `__main__` guard | FAIL (left red as instructed) | 0.85 s | | |

Still red, and why:
- `test_run_file_produces_a_measured_report_row[search_det]` and `[search_ai]`: `assert row["verdict_candidates"] > 0`. The search finds zero verdict candidates on an open shell (`v4_fix_poles` changes nothing; `op_sew` rejects the 0.05 rung). This is Task 36. Not chased. My fixes did not change it.
- `test_repair_v4_is_callable_from_a_script_without_a_main_guard`: the guard question, yours.

## Where the 25 s goes
Pool startup. `WorkerPool.__init__` spawns workers SEQUENTIALLY, each waiting for its "ready" (a cold OCP import) before the next spawns. Measured: `WorkerPool(1)` 6.0 s, `WorkerPool(4)` 23.9 s, so ~6 s per worker, linear. Read of the file 0.15 s, `default_judge_for` 0.37 s, input verdict 5 s (cold), staged 1.6 s, pool close 1.4 s. Caveat: this ran as linux/amd64 under emulation on an arm64 Mac, so the absolute 6 s per worker is inflated, but the structure (sequential, linear in `size`, `_workers()` = min(4, cpus)) holds anywhere. Spawning the workers concurrently and waiting once would cut startup to about one worker's cost. Also: `repair_v4()` builds and closes a pool PER FILE, so this cost is paid on every one of 10,887 files unless the pool is reused. Not changed here.

## The four items

1. **All four arms selectable.** `repair_v4.ARM` accepts `input_only`, `staged`, `search_det`, `search_ai` (`VERDICT_ONLY_ARMS` for the first two; unknown arm is a `ValueError` before any pool exists). `input_only`: judge the input, `SearchResult(changed=False, reason="input_only")`, no `run_staged`, no `_find_best`, passthrough byte for byte (unit test fails if it runs the staged healers; mutation below). `staged`: `run_staged` then one more `judge.full` on the staged workspace, `reason="staged"`, delivered only through the same `deliverable` gate (a worse staged output is not shipped).
2. **`search_ai` route.** Comes from `repair_v4.ROUTE` (default None) falling back to `REPAIR_V4_PROPOSE_ROUTES[0]` (`openai:gpt-6-astra`). The engine does NOT call `select_route` per file: a probe is a model call on each of 10,887 files, and Task 31's fake transport answers `propose`, not `report_colour`, so a probe could not pass under it. A wave probes once per shard (`model_route.probe_route_chain`) and pins the winner into `ROUTE`; the router's price check refuses an unpriced route before dialling. The ledger is new `WaveLedger.for_file(cap)`: in-memory, capped at the file's `Budget.model_usd`, provenance `"file"` (the proposer now accepts `shard`, `test`, `file`). `usd_ceiling` = `Budget.model_usd`, so the proposer's own (0, $1.00] guard refuses a larger request (tested). `Budget.model_calls` is the turn cap.
   - **Found while making the wire assertion run: a fifth-and-a-half "built, never connected" bug.** The proposer never put a request on the wire at all: `sites.py` stored `graph["unmeasured_pairs"] = []` for a site that was never clustered (an `unlocated` site, which is exactly this defect's only site), and `observation()` did `int(list)`, a `TypeError` the search swallowed as `generator_error`. Fixed at the source (`sites.py` ~684: the default is `0` for that key). Without it no request ever reached the router.
   - After the fix: through `repair_v4()` 4 requests on the wire and 4 `ledger.admit` calls, all ok (new native test, counting `WaveLedger.admit`); through `arms.run_file` search_ai 4 wire / 4 admits / $0.06 recorded. Task 31's own admit assertion is still behind its measured-ness assertion (Task 36), so it does not execute, but the equal counts it would check are shown here.
3. **`h_mm`.** `H_MM` deleted; `_make_judge(pool, ws)` calls `search.default_judge_for`. Seen by the judge in the e2e test: 1.7320508 mm (not 1.0). Two-scale evidence on the real kernel (`test_native_repair_v4_h_mm_scales.py`): 10 mm box -> 1.7320508 mm; 10 000 mm box -> 1732.05 mm; ratio 1000 (rel 0.05); both equal `1000 * base_size(diag/1000)`. Hermetic version at diag 17.32 mm and 17 320 mm in `test_engine_arms.py`.
4. **`V4Result` contract.** `verdict_summary["search"]` now carries `reason`, `changed`, `clean`, `verdict_candidates`, `inconclusive_candidates`, `infrastructure_failed` (from the `SearchResult`, one `_search_summary` helper). New property `V4Result.file_unmeasured` IMPORTS and calls `measurement.file_unmeasured`; a summary with no `search` block (deadline passthrough) reads as unmeasured. No second definition of the rule exists. Existing fields and the `repair_v4()` signature are unchanged.

## Mutations (each applied alone, test run, restored; clean tree checked)
- `input_only` runs staged (`if ARM == "input_only"` -> `if False`): `test_input_only_touches_nothing...` fails.
- `input_only` removed from `VERDICT_ONLY_ARMS`: unit `test_input_only...` fails; native `[input_only]` fails.
- `_make_judge` back to `default_judge(h_mm=1.0)`: unit `test_the_judge_is_sized_from_the_part...` fails; native `test_repair_v4_sizes_the_mesh...` fails.
- `h_mm_for` units slip (`base_size(diag_mm)` instead of `/1000`): the two-scale unit test fails.
- `_search_summary` drops `verdict_candidates`: `test_the_search_summary_carries...` and `test_input_only...` fail.
- `V4Result.file_unmeasured` negated: `test_the_rule_is_applied_not_restated`, `...carries_every_input...`, `test_input_only...` fail.
- route -> `[1]`: native `...admits_every_request_once` fails (the pinned-route test correctly still passes).
- `usd_ceiling` clamped instead of refused: `test_a_file_never_spends_past_its_ceiling...` fails.
- `sites.py` default back to `[]`: unit `test_an_unlocated_site_is_observable...` fails; native wire tests fail (2).
(An earlier mutation pass was invalid because the native image has no `git` to restore with, so mutations compounded; discarded and redone with `cp` restore.)

## Unit-tier id diff (`tests/unit/cad/repair_v4` + `tests/unit/dev`, validation image, `-rfE`)
Base `cada9133`: 1 failed, 1738 passed, 2 skipped. Head: 1 failed, 1749 passed, 2 skipped (+11 = my new tests).
`comm -23 base head` (fails only on base): **empty**. `comm -13 base head` (fails only on head): **empty**.
The single failure on both sides is `test_the_settings_tier_passes_with_every_required_setting_hostile` (known; it wraps the `no_machine_specific_path` failure). Both sides otherwise zero, so the diff proves little beyond "nothing that already passed broke"; the evidence for the change is the native runs and mutations above. The head run was cloned at `35ab97e1`.
ruff clean on all changed files; mypy clean on the 4 changed `src` files (one C416 style nit in Task 31's file left, not mine to edit).

## Tests changed or added
- Added: `tests/unit/cad/repair_v4/test_engine_arms.py` (10), `test_sites.py::test_an_unlocated_site_is_observable_by_the_proposer`, `tests/native/test_native_repair_v4_ai_wire.py` (3), `tests/native/test_native_repair_v4_h_mm_scales.py` (1), plus Task 31's file verbatim.
- Signature follow-up, not weakened: the fake `_make_judge` in `tests/unit/cad/repair_v4/test_engine_entry_exit.py` and `tests/native/test_native_repair_v4_engine_entry_exit.py` now accepts `(pool, ws)`.

## Commands and real exit codes
- e2e + engine entry native (`pytest ... --durations=0 > log`, exit read from a file): **exit 1** (3 failed, 9 passed, 249 s). Earlier first e2e run had its exit read through `tail` and is not claimed.
- Unit base and head runs: wrote to files, summaries read from the logs ("1 failed, 1738 passed" / "1 failed, 1749 passed"); the shell loop's `echo $?` lines were lost to a backgrounded timeout, so the pytest exit codes for those two runs are inferred (non-zero because of the known failure), not read.
- Native ai_wire (3 passed, 89 s), h_mm scales (1 passed, 12.7 s), mutation runs and unit new tests (28 passed): outcomes read from pytest's summary line; those docker invocations piped through `tail`/`grep`, so I did not read pytest's exit code for them.
- Task 31's test pass counts come from pytest summaries. All container runs used `git clone --no-hardlinks` to `/tmp/t35*`, absolute `PYTHONPATH=/w/src`.

## Known gaps / notes for the controller
- `search_ai` on the engine path needs a router installed (service composition does it; a wave uses `model_route.install_router`). With none, rounds fail and the proposer degrades; I added no pre-check.
- `ROUTE` is a module attribute like `ARM`; production selection of either is still by code, not by setting.
