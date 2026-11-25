# Task 46 report: the search's patience scales with the work

Branch `sdd/task-46` from `0f02d064`. Code head `e15c8354` (the commit carrying this report follows it). No model, no GCP, no VM, no push, no merge, no `pkill`.

## Headline, plainly
**A 3-hole part now reaches `make_volume` and ends `clean` on the real kernel, but not at the op budgets in use. It needs ~70 ops; the e2e budget is 12 and the production `CAD_REPAIR_MAX_OPS` default is 6.** At 12 ops the search stops on `ops` with `make_volume` never evaluated. So the obstacle to the benchmark recording a 3-hole part as a failure is now the op budget plus the search order, not patience. I did not widen any budget; the native acceptance test grants 80 ops to the test only so patience is the sole variable, and a second native test pins that at 12 ops the stop reason is `ops`, not `patience`. This needs your decision (see "Open decision").

**Second finding: the tie rule makes the patience scaling mostly redundant on the 3-hole part.** Restoring constant patience (M1) leaves the native 3-hole test green: because every `fill_loop` ties the best, flat never accumulates. Scaling matters only where rounds are NOT progress (dead-end siblings tried before the way out); that has its own unit test, which does fail under M1.

## What changed (`src/meshpipeline/cad/repair_v4/search.py`)
- `search(..., patience: int | None = None)`. `None` = scaled; an int is fixed as before (all existing tests pass explicit values).
- `open_site_count(sites)`: the **closed free loops** layer 1's open finding carries, `max` over `open` sites of `graph["free_loops_total"]` (the UNCAPPED count; `free_loops` itself is capped at 6), falling back to `len(free_loops)`, and never below the number of `open` sites (when the loop call failed). Why this: it is exactly the number of `fill_loop` candidates the sweep can make, i.e. the number of non-improving fill rounds a repair may need before `make_volume`. `S@invalid`/`S@unlocated` are not holes and are not counted. All open sites carry the same part-wide loop list, so they are not summed.
- `scaled_patience(n) = max(3, n + 2)` (`PATIENCE_FLOOR`, `PATIENCE_SLACK`). Settled **once, from the start state's own sites at its first expansion**, recorded as a `patience` trace event (`open_sites`, `patience`, `floor`). Not recomputed per state, so a descendant cannot raise its own budget.
- Progress: a round is progress if it improved best OR a **confirmed new child scores >= the best so far**. "New" is `_fingerprint` (Task 45: sorted ids + `ws.signature`, signed shell volumes), so a state seen already is a `duplicate` and never counts; an orientation-only change is new.
  - **Compared with the best, not the parent.** My first version compared with the parent and `test_search.py::test_patience_stops_flat_search` caught it: a child of a worse state that ties that worse state ran to the op budget. Now a search wandering below its best still runs out of patience (`test_a_state_that_only_ties_a_parent_worse_than_the_best_is_not_progress`). Worse children never count. Only confirmed (full-tier) children count, so a cheap-vs-full tier mismatch cannot fake a tie.

## What stops a pointless loop; worst case
- Tied-but-useless new states ARE progress by this rule, so **patience cannot stop them; the op and wall budgets do** (pinned by `test_new_tied_states_without_end_stop_on_the_op_budget...` = 15 of 15 ops, and the wall-clock twin).
- Worst-case patience is `max(3, loops+2)`: unbounded in principle (500 loops -> 502, tested), but never effective beyond the budgets: a round adds a state only by spending an op, so at most `ops + 1` rounds run. With a 6-op budget and patience 502 the run ends at 6 ops (`test_scaled_patience_never_exceeds_what_the_op_budget_allows`). Budgets untouched.
- Genuinely stuck still stops on patience, far under the op budget: all-worse children (ops <= 6 of 1000), all-duplicates, and the worse-parent tie case.

## The 3-hole test and the numbers
Fixture `three_hole_box()` (`tests/native/_v4_flush_patch_part.py`): the flush-patch box with all three 3x3 mm squares deleted (three free loops, each ~3.4% of the diagonal). `tests/native/test_native_repair_v4_patience.py`, real `repair_v4()` on the det arm, real pool/judge/panel, nothing faked:
- `test_a_three_hole_part_reaches_make_volume_and_ends_clean`: `make_volume` evaluated, reason `clean`, delivered. Passes (ops=80 grant). Measured by hand: ops=12 -> `ops`; 24 -> `ops`; 40 -> `ops` (23 verdict candidates, 6 expansions, depth-3 state reached at about op 34 but not expanded); 80 -> `clean` at 70 ops, 40 verdict candidates, path S0,S1,S4,S10,S20.
- Why so many ops: tied states rank equal and the heap pops FIFO, so the search is breadth-first over fill subsets, and each expansion spends ~7 ops of which ~4 are waste (`sew`, two `make_volume` declined, `orient_and_close` duplicates/children).
- `test_with_the_default_twelve_ops...`: at 12 ops reason is `ops`, `make_volume` not evaluated, patience event `open_sites 3, patience 5`.
- `test_the_one_hole_case_still_runs_at_eight_ops_and_five_verdict_candidates`: **8 ops, 5 verdict candidates, `clean`, unchanged** (also `open_sites 1`, patience 3). Task 42's `fixable` e2e tests pass.

## Mutations (each restored; tree clean)
| mutation | unit (`test_search_patience.py`) | native 3-hole test |
|---|---|---|
| M1 constant patience | 3 fail (dead-ends-then-way-out, patience recorded, 502 cap) | **passes** (tie rule carries it; not what you predicted) |
| M2 no tie progress | 3 fail (tied chain of 6, op-budget stop, wall stop) | **fails**: reason `patience`, 35 ops, no `make_volume` |
| M3 both (= old behaviour) | 7 fail | **fails**: `patience`, 22 ops, no `make_volume` |

## Id diff (clean clones, no `v4-` in path, absolute PYTHONPATH). Head `e15c8354` vs base `0f02d064`
- Unit (validation image, `tests/unit/cad/repair_v4 tests/unit/dev`): head 1879 passed, 1 failed; base 1865 passed, 1 failed. Failure on both: `test_the_settings_tier_passes_with_every_required_setting_hostile`. `comm -23 head base` (head-only): EMPTY. `comm -13 head base` (base-only): EMPTY. +14 passes are my new tests. **Proves little**: both sides carry only the known failure.
- Native (`tests/native/test_native_repair_v4_*.py`, `test_native_v4_inject.py`): head 254 passed, 1 failed; base 251, 1 failed. Failure on both: `test_repair_v4_is_callable_from_a_script_without_a_main_guard`. Both `comm` directions EMPTY; +3 are mine. Proves little, same reason.
- The first head unit run (before the best-vs-parent fix) had `test_search.py::test_patience_stops_flat_search` failing; it is the defect described above, fixed.
- `test_no_machine_specific_path_reaches_the_template` appears only inside the onboarding test's captured subprocess output (same as base).

## Commands and real exit codes (each redirected to a file, `$?` read separately)
- 3-hole test red on base logic: rc 1. Final native `test_native_repair_v4_patience.py` (3 tests): rc 0. Unit `test_search_patience.py` + `test_search.py` + `test_search_wiring.py` (validation image): 111 passed, rc 0; ruff on search.py and new tests: rc 0; mypy on search.py: rc 0 ("Success: no issues found in 1 source file"). ruff was not in the native image, so lint/mypy ran in the validation image.
- Gates: unit head rc 1 / base rc 1; native head rc 1 / base rc 1 (known failures only). `git` is absent in the native image, so `git config` printed "command not found" and the run continued (clones need no safe.directory for pytest).
- Hand drives (`/tmp/t46x/drive.py`, ops 12/24/40/80): rc 0 each; stop reasons above.

## Open decision (yours)
To make a 3-hole part repairable at a real budget, something other than patience must change; options, not done: (a) tie-break equal-ranked states toward the deeper one so the chain is explored depth-first (about 4 expansions instead of 8); (b) stop proposing ops that cannot apply (`make_volume` on an `open` site, `sew` that is always declined, `orient_and_close` duplicates), which cut ~4 of ~7 ops per expansion; (c) raise `ops`. Also note the production default `CAD_REPAIR_MAX_OPS=6` is below the 8 ops Task 42's one-hole repair needs, which predates this task and is worth checking before any benchmark.

## Files
`src/meshpipeline/cad/repair_v4/search.py`; tests `tests/unit/cad/repair_v4/test_search_patience.py`, `tests/native/test_native_repair_v4_patience.py`, `tests/native/_v4_flush_patch_part.py`.
