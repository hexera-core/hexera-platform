# Task 51 report: a search arm no longer discards the staged healer's repair

## Result
The strict xfail `test_a_search_arm_delivers_the_staged_healers_gain_when_the_search_adds_nothing` (search_det, search_ai) now PASSES. The xfail marker is removed and the test body is unchanged.

Four delivery cases, each parametrised over search_det and search_ai (tests/unit/cad/repair_v4/test_engine_arms.py, real `_find_best`, real `deliverable`):
1. Healer improves, search adds nothing: healer state delivered (the xfail case). PASS.
2. Best state worse than input (`test_a_state_worse_than_the_input_is_still_refused_after_a_healer_only_change`): input passed through, no write_step. PASS.
3. Input already clean (`test_a_clean_input_is_never_replaced_by_a_search_arm`): never replaced. PASS.
4. Search improves on healer (`test_a_search_that_improves_on_the_healer_is_delivered`): search state delivered. PASS.
Plus `test_nothing_changed_at_all_delivers_nothing` (healer is a no-op and search adds nothing: passthrough).

## The fix
src/meshpipeline/cad/repair_v4/__init__.py, `_find_best`: after `search(...)`, if `not result.changed and result.best.brep_path != ws.brep_path`, set `changed=True` (dataclasses.replace). `ws` is the INPUT workspace; same test `_verdict_only` already uses for the `staged` arm. `search.deliverable` is untouched.

Judging `changed` against the input is right. `deliverable` already compares `result.best_verdict` with `input_verdict` (panel passes, never replacing a clean input). The bug was only that `changed` was relative to the staged start. When search adds nothing, `best_verdict` is the verdict of the staged state itself (search's s0), so the gate compares exactly "staged vs input". A regression is still refused.

Engine vs eval: they now agree. Both run staged first and judge delivery against the input. I did not read arms.py line by line; the agreement is by the shared contract and by the engine's `staged` arm and search arms now using the same input-relative `changed`.

Extra verdict: NONE needed. The input verdict is already computed in `_run_search` and passed to `deliverable`; `_find_best` needs nothing new, and search's own s0 verdict is the staged verdict. Cost: zero extra panel runs.

## Mutations (each restored; git status clean after)
- M1 never set changed (`if False:`): breaks the xfail-case test [search_det], [search_ai].
- M2 always set changed (`if True:`, permissive): breaks `test_nothing_changed_at_all_delivers_nothing` [both].
- M3 disable deliverable's panel-passes comparison (search.py): breaks the regression test [both] and the existing `test_staged_that_is_worse_than_the_input_is_not_delivered`.
The clean-input and search-improves cases are pinned by `deliverable`'s `input_verdict.clean` guard (not mutated) and normal path; not mutation-checked individually.

## Id diff (tests/unit/cad/repair_v4, base eea4a823 vs head, validation image)
base: 649 passed, 2 xfailed, 0 failed. head: 659 passed, 0 xfailed, 0 failed.
`comm -23 base head` (failing in base only): empty. `comm -13 base head` (failing in head only): empty. Both sides have zero failures, so this diff proves little; the evidence is the passes, the 2 xfails becoming passes, and the mutations. Native tier (meshpipeline-mesh:native) NOT run: this change is hermetic (fakes) and I ran the unit dir in the validation image. The two known failures named in the brief were not in this directory.

## Commands and exit codes
- `git worktree add -b sdd/task-51 ... eea4a823`: 0
- pytest test_engine_arms.py + test_engine_entry_exit.py (validation image, PYTHONPATH=/work/src absolute): exit 0, 38 passed
- mutations M1, M2, M3: pytest exit 1 each (expected)
- `ruff check` on both changed files: 0. `mypy src/.../repair_v4/__init__.py`: 0 ("no issues found in 1 source file"). Run in validation image.
- `ruff format --check`: 1, but it also reports both files at base eea4a823 (repo is not format-clean there); not caused by this change.
- full tests/unit/cad/repair_v4 base and head: exit 0 each (read from a redirect, not a pipe)
- Head commit: 7fccd9de (report committed after it).

## Notes
Nothing was pushed or merged; no model calls; no process or container killed. The T2 smoke's `search_det`/`search_ai` clean counts are lower bounds under the old code, as Lane C said; they will rise after this lands.
