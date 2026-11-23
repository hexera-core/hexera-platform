# Task 44 report: why the CRS v4 search is timing-sensitive

## Mechanism (found, reproduced, failing trace captured)

`stages.step_for` (the BREP -> STEP export every verdict goes through) named a new export
`state_{len(_STEP_FOR)}.step` BEFORE the write and registered it in `_STEP_FOR` AFTER. The search calls it from
`ThreadPoolExecutor` threads (one `judge_cheap` per candidate, `judge.full` per confirmed child). Two threads that
entered together chose the SAME path; the second write replaced the first, so one candidate was judged on
ANOTHER candidate's geometry. The wrong path was then memoised under the right candidate's key, so the full-panel
confirmation repeated the wrong verdict. Whether two threads overlap depends on load. It is a race, not a budget.

Captured failing run (search_det, `/tmp/t44out/M_4_1`, and `/tmp/t44out/rep_a` run 5; kept outside the repo):
- S0 expansion identical to a passing run (fill_loop evaluated; orient_and_close x2 duplicate; sew, make_volume rejected).
- S1 expansion: `make_volume` evaluated with cheap score `[1,0,-1.0,..]` / `[0,0,-1.0,..]` (passing run: `[1,1,0.295,..,-1]`).
  The `make_volume` BREP itself (saved: `rep_a/run5/out0.brep`) is a valid solid, volume 240000, forward-oriented: the
  geometry was good, the verdict was of a different file. `orient_and_close` and `make_volume` evaluate in parallel
  on S1 and are exactly the pair that collide.
- Full confirm of S2: `[0,0,-1.0]` not clean (memoised wrong STEP).
- S2 expansion: `orient_and_close` returns the correct geometry `[1,1,0.295,..]` but the dedupe (`_fingerprint`,
  geometry only) says "duplicate of S2" and discards it. S2 is never good, so no improvement, patience 3 -> stop `patience`,
  `verdict_candidates` 6 (the extra "ok" is that orient_and_close on S2 plus the wrongly-judged make_volume).
- Passing run: 5 candidates, `clean`, best path S0-S1-S2.
Corroboration: a loaded run also logged `read_step_xcaf: STEP file could not be read: .../state_35.step` (a reader hitting a
file mid-overwrite), and one load run ended `clean` with 7 candidates (a different path, also race-perturbed).

Threads from the brief that were ruled out: frontier ordering (results are consumed in submission order, ties broken by a
monotonic counter, `search.py` ~l.1012); completion-order dependence of results; render.view (not used by search_det,
which also fails); patience accounting (3 flat rounds is correct arithmetic once S2 is mis-scored; it is the symptom);
timeouts/retries (no timeout, retry or inconclusive in any captured trace: `inconclusive_candidates` 0); fingerprint
(deterministic; see "also" below). Sequential reruns of fill_loop -> make_volume -> cheap verdict were identical 6/6 fresh
pools, 12/12 fresh pools under load, 12/12 warm 4-worker pool under load.

## Is it non-determinism or budget fragility?

Non-determinism that should not exist. Fixed. (Budget fragility is a separate, real property, below.)

## Fix (head commit d152dd61 on sdd/task-44; test commit before it)

`stages.step_for`: take `_STEP_LOCK`, choose the name from an `itertools.count()`, register under the lock; the
kernel write runs outside the lock. Test `tests/unit/cad/repair_v4/test_step_for_concurrency.py` forces the interleaving
with a barrier in a fake pool: red on base (both BREPs got `state_0.step`), green after.

## Evidence it was this

Same load (5 busy-loop containers + 2 concurrent copies of the gate test, emulated amd64 on arm64, plus Task 43's probes):
- before: 2 failed of 16 gate-test runs (search_det and search_ai, both `patience`, 6 candidates); in-process loop 2 bad of 20.
- after:  0 failed of 24 gate-test runs; in-process loop 20/20 `clean` with 5 verdict candidates.
Caveat: rates are for this load; 0/44 is evidence, not proof, and I did not attribute every historical failure.
Heavier load (12 burners + 4 concurrent) produced a DIFFERENT failure: `kernel worker did not start within 30s`
(`SearchUnavailable`) - the pool start timeout, an infrastructure error that surfaces loudly, not a wrong answer.

## Mutation

`next(_STEP_SEQ)` -> `len(_STEP_FOR)` (the old naming): new test fails (1 failed). Restored: passes.

## Reported, not fixed

1. Budget fragility (Task 42's finding stands): `fill_loop` does not improve the score, `make_volume` is the improving step,
   patience is 3 non-improving expansions. A part with 3+ holes, or any path that needs >=3 expansions before the first
   improvement, stops on `patience`. In THIS case S0 and S1 are flat and S2 (make_volume) improves, so the working path has the budget it needs (patience 3, flat reaches 2); there is one round of slack. Recommendation for the owner: count patience only for expansions whose children all fail to
   advance a monotone progress measure (e.g. free-edge count / open-site count), not for score-flat expansions, or set
   patience to (holes + 2). I did not tune it.
2. The dedupe `_fingerprint` is centroid+box only and orientation-blind: a wrongly-oriented solid dedupes away its own
   orient_and_close fix. It did not cause this flake (the fault above fed it), but it would hide real fixes. Not changed.
3. `WorkerPool(start_timeout_s=30)` fails the whole run under heavy load (see above).
4. ruff and mypy are not installed in `meshpipeline-mesh:native` or on the host, so neither was run; mypy-on-changed-file rule
   NOT satisfied here. The change is stdlib-only (`threading`, `itertools`).

## Is the engine safe to measure with?

Measured at the load above: yes, after this fix; before it, no. Any T2-smoke number produced with the base engine under
concurrency (workers>1, e.g. the real 4-worker run) could include candidates judged on another candidate's geometry, which
would show as a spurious `patience`/`not clean` or a different candidate count. Numbers from before this fix should not be
compared with numbers after it. Remaining risk: pool start timeout and the other shared module state I did not audit
(`_ORIGINAL_FACTS`, `original_facts`, verdict cache memory store were read, not stress-tested).

## Commands and exit codes (all docker, image meshpipeline-mesh:native, PYTHONPATH absolute)
- baseline gate test, uncontended: exit 0 (2 passed)
- heavy load x4 + 12 burners: all 4 exit 1 (`worker did not start within 30s`; not the target failure)
- moderate load (M, 4 rounds x2): exits 0,0,0,1,0,0,0,1 (the two failures are `patience`, 6 candidates; traces kept)
- rep_a/rep_b (in-process 2x10, load): 1 `patience`/6, 1 `clean`/7, 18 `clean`/5
- new unit test on base: exit 1; after fix: exit 0; mutated: exit 1; restored: exit 0
- `tests/unit/cad/repair_v4` full: exit 0 (`-q -q` hid the summary line; no failures printed)
- post-fix F loop (6 rounds x2): all exit 0; rep_c/rep_d: 20/20 clean, 5 candidates
- ruff / mypy: not available in the image (exit 1, "No module named"); not run
- other known-failing tests not run
