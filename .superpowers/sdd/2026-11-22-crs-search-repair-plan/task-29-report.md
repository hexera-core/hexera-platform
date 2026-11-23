# Task 29: fake panel module drifted from the real contract

## What changed
Only `tests/unit/cad/repair_v4/test_search_wiring.py`. `assemble.py` is untouched; its call-time read of
`panel.INFRASTRUCTURE_KIND` stays as Task 27 made it (no getattr default).

- `fake_panel_module(run_panel)` builds the stand-in from the real `verdict.panel`: every public non-callable,
  non-module name is copied with its real value (so `INFRASTRUCTURE_KIND == "infrastructure"` and any constant
  added later). Callables are never copied (a real one would mesh); the test supplies `run_panel`.
  Both fake sites (`real_checker` fixture, the clean-rule test) use it, so all four tests share it.
- Guard `test_the_fake_panel_module_covers_everything_assemble_reads`: AST-scans `assemble.py` for every `X` in
  `_panel_module().X` (also via a local alias), sanity-checks the scan found `INFRASTRUCTURE_KIND` and
  `run_panel`, and asserts the fake has them all. Failure message names the missing attributes and the fix.
- No member count is assumed anywhere (Task 28's fourth member is irrelevant to these fakes).

## Why this measure
Deriving constants from the real module removes the drift for values; the AST guard catches the other
direction (a new attribute that is not a public constant, e.g. a function, that assemble starts reading), with
one obvious failing test.

## Head
054b2acf on sdd/task-29 (base 8eac6fb2); report committed on top (force-added; .superpowers is gitignored, earlier reports are tracked).

## Mutation
In a clone, excluded `INFRASTRUCTURE_KIND` from the helper. Result: 5 failed. The guard fails with
"fake_panel_module lacks ['INFRASTRUCTURE_KIND'], which assemble.py reads off the panel module; ...", plus the
four wiring tests with the raw AttributeError (as they must, the fake is broken; the guard is the readable
one). Clone restored with `git checkout`.

## Id diff (tests/unit/cad/repair_v4, docker meshpipeline-validation:local, clones with no `v4-` in path)
- base 8eac6fb2: 4 failed, 455 passed (exit 1). head: 460 passed (exit 0).
- `comm -23 base head` (fails only on base): exactly the four named tests.
- `comm -13 base head` (fails only on head): empty.
- `-rs`: zero SKIPPED lines on both; skip lists identical (diff exit 0).
- Passed count 455 -> 460: the four fixed + the one new guard.

## Task 3 guards
`test_no_verdict_module_but_panel_spells_the_infrastructure_literal` (AST scan of src) and the
patch-then-import subprocess test (`_FOLLOW`, test_panel.py:508) both pass in the full run, for the same reason
as before: they read `src/` and run a subprocess, neither touches test_search_wiring.py or assemble.py.

## Commands (exit codes)
- `git worktree add ... -b sdd/task-29 8eac6fb2`: 0
- `git clone --no-hardlinks` head and base clones, base checked out at 8eac6fb2
- `docker run ... pytest tests/unit/cad/repair_v4 -q -rfEs` redirected to /tmp/t29-head.log: exit=0; base log: exit=1
- mutation run (4 wiring tests + guard) redirected to /tmp/t29-mut.log: exit=1 (expected)
- `diff` of skip lists: 0 (no skips on either side)
