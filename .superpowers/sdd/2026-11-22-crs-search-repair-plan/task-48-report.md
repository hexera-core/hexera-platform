# Task 48 report: the stale reply, and the panel stratification key

Branch `sdd/task-48`, base `97d978cf`. Not merged, not pushed. No model called. Commits: `870c1ef2` (pool), `66155aae` (panel family), `f0465442` (stratum key), `237891f0` (test literals), plus this report (head = `git log -1`). Everything ran on VM `dev-crsv4-gate-2` (us-central1-b, native x86), gates in the images below, clones under `/tmp/w-*` (no `v4-` in path), `PYTHONPATH=/w/src` absolute.

## 1. The real reproduction (real `WorkerPool`, spawned worker, real pipe, `meshpipeline-mesh:native`)

Script: file A makes a `pool.call("slow_echo", {sleep_s: 1.5})`; a SIGALRM deadline (exactly Task 43's `FileTimeout`: an exception raised inside `pool.call`) fires at 0.3 s. The abandoned worker finishes and writes its reply. File B then makes its own call and reads `["echo"]["units"]`. (`slow_echo` is a test-only kernel function, registered only under `HEXERA_TEST_KERNEL_FNS`.)

**Old pool (`97d978cf`), exit of the script as printed:**
```
file A: FileTimeout raised mid pool.call
file B result: ok {'echo': {'sleep_s': 1.5, 'verdict': "A's"}, 'pid': 11} None
B's echo is B's own: False
B CRASHED: KeyError 'units'
```
Note the status: **`ok`**. B was handed A's reply as a successful answer. It crashed only because the keys differed; had they matched, it would have been a silently wrong value. (This is the Task 43 failure, `KeyError: 'units'`, reproduced.)

**New pool (`870c1ef2`):**
```
file A: FileTimeout raised mid pool.call
file B result: ok {'echo': {'n': 'B', 'units': 'mm'}, 'pid': 12} None
B's echo is B's own: True
B reads ['echo']['units'] -> mm
```
B got a fresh worker (pid 12, not 11) and its own reply. Unit tests `test_a_call_abandoned_by_a_caller_deadline_does_not_poison_the_next` and `test_a_reply_for_another_request_is_never_returned_as_a_result` in `tests/unit/cad/repair_v4/test_worker_pool.py` drive the same real pool; the repro script itself is not committed (it is above, reproducible from the test).

### Root cause
The pool already killed a worker on ITS OWN timeout. The leak was any other exit from `call`: an exception escaping mid-flight (the driver's SIGALRM, Ctrl-C). The `finally` put the still-busy worker back in the idle queue; its reply later sat in the pipe and the next caller's `recv()` took it. Task 43's "900 s FileTimeout mid pool.call" is this path.

### The fix, and why it is right rather than merely sufficient
Two layers, both in `worker_pool.py`, callers untouched:
1. **Retire on abandonment (prevention).** If `call` is left by anything other than a recorded outcome (reply, timeout, death), its worker is killed and replaced. Right rather than merely sufficient, because the alternatives are worse: *draining* means waiting for a computation the caller already gave up on, which is exactly the one that overran (possibly a hang), so the next file would inherit its latency or block forever; and the abandoned work's result is of no use. Killing is also what the pool already does for its own timeouts, so every abandoned call now has one outcome: the worker is gone.
2. **Request id (detection).** Requests are `(id, fn, payload)`, replies `(kind, id, body)`. A reply whose id is not the one this call sent is never returned as a result: the call reports `crashed` ("reply for request N, expected M; worker replaced") and the worker is replaced.

**A stale reply can never be silently accepted:** the id comparison is structural, independent of layer 1. If layer 1 had a hole, the result is a loud `crashed` CallResult (which the panel already treats as such), not a wrong verdict. A malformed reply raises, retiring the worker via layer 1's `finally`.
Task 3's ruling is untouched: pool timeouts remain infrastructure, one retry per file, in the callers.

### Mutations (native image, `test_worker_pool.py`)
- Layer 1 off (`if not clean:` -> `if False:`): `test_a_call_abandoned...` FAILS (`assert 'crashed' == 'ok'`), exit 1, and the repro prints `file B result: crashed ... reply for request 1, expected 2; worker replaced` -> the id check turns the old silent/KeyError path into a loud, named failure. (The caller still gets no answer for B; that is the loud outcome, not a wrong one.)
- Layer 2 off (`if got_id != req_id:` -> `if False:`): `test_a_reply_for_another_request...` FAILS (`assert 'ok' == 'crashed'`), exit 1; the repro still passes because layer 1 prevents the stale reply.
- Both off = the old pool: both new tests fail, KeyError 'units' returns (section above).
- Unmutated: 11 passed, PYTEST_EXIT=0.

## 2. Panel family and the stratification key

### Recognition (done, cheap)
`reprobe.panel_family`/`member_family`: a failing member's family is its `error_kind` when that names a cause (`low_quality`, `no_volume`, `overlapping_facets`, `edge_through_face`, `periodic_surface`, `netgen_meshing_failed`), else its message ("HXT 3D mesh failed" -> `hxt_3d_failed`), else `timeout_crash`. The panel's label is the best-ranked family among ALL failing members (named geometric cause > netgen failure > timeout/crash > HXT > low_quality > other), ties alphabetical, so it no longer depends on member order. `inject._FAMILIES` (the injector calibration table) is deliberately untouched.

Over Task 43's 33 panel failures (replayed from `PROVISIONAL_probe_records_x86.jsonl`, same inputs):
- before: `other` 31, `periodic_surface` 2
- after: `low_quality` 12, `overlapping_facets` 8, `edge_through_face` 5, `netgen_meshing_failed` 2, `periodic_surface` 2, `no_volume` 2, `timeout_crash` 1, **`other` 1**.

Caveat: the stored records keep `error_kind` but not message text, so this replay cannot exercise the HXT text rule; `hxt_3d_failed` occurs 0 times in it. The 1 remaining `other` is a file whose only failing member has error_kind `other` (18 gmsh_prod members have it); live it would become `hxt_3d_failed` if the message is the HXT text, which I could not confirm from stored data. That family is supported by Task 43's observation, not by this replay. Valid-layer families are unchanged (already real).

### Ruled change: the stratum key (substance of the item)
`build_splits` now stratifies by `(first_failing_layer, failing panel members, coarse kind, source)`:
- failing members = count of non-pass members in the row's panel summary (panel layer only; `-` for a valid-layer row, whose panel is not a verdict on the file). Measured: 1 member 5 files, 2: 5, 3: 17, 4: 6 among panel failures.
- coarse kind: panel -> `geometry` / `quality` / `mesher` (all three occur); valid -> `body_overlap` / `open_shell` / `self_intersection` (15/7/7) / `valid_other` (brep_invalid 2 + round_trip_invalid 2); faithful -> `faithful`.
- `source` kept. The fine `family` stays on the row for a human.

Strata over the 65 failing files in Task 43's sample (after dropping 2 no_solids... the sample's 66 less the unbounded/no_solids rows as `build_splits` does): old key 15-16 strata, new key 27 (panel strata: 1/quality/abc_pass 2, 3/geometry/abc_fail 10, 4/geometry/abc_fail 4, ... ; valid: body_overlap abc_fail 12, abc_pass 2, occt 1; open_shell 3/1/3; self_intersection 4/2; valid_other 2/2).

### Does it make dev and held-out comparable? Honestly: not demonstrable on this sample
Mean total-variation distance, dev (30) vs held-out (25) drawn from the 65, over 60 seeds (lower = more comparable):

| key | strata | TVD on (layer, #failing) | TVD on coarse kind | TVD on source |
|---|---|---|---|---|
| old (layer, family, source), panel family all `other` | 15 | 0.125 | 0.116 | 0.073 |
| new (layer, #failing, coarse, source) | 27 | 0.242 | 0.200 | 0.092 |
| (layer, #failing, source) | 14 | 0.150 | 0.213 | 0.025 |
| (layer, #failing, coarse) | 15 | 0.100 | 0.073 | 0.142 |

With 65 files and ~85% of them drawn, 27 strata have 1-3 files each and largest-remainder rounding dominates, so the finer key is noisier here. This is a small-n effect I cannot rule out or confirm with this data; at the plan's scale (hundreds of failing files) strata are populous. **Not proven comparable; recommend re-measuring TVD on the full re-probe before the split is frozen, and if strata stay sparse, drop `source` or `coarse kind` rather than fragment.** I kept the ruled key as instructed.

### First-failing-member labelling
Dropped as a label: the panel disagrees with itself on 51 of 73 files, so the first member is arbitrary (gmsh_prod 31 of 33). Stratification now uses the count of failing members (stable) and a coarse kind taken from the best-ranked failing member (order independent). Mutation: reverting `panel_family` to first-member fails 5 tests; removing the HXT rule fails `test_hxt_failure_text_is_a_family_not_other`.

### Your questions
- **Does anything in the splits path infer "clean" from `source`? No.** `certified` in `build_splits` is `first_failing_layer is None and verdict_summary["clean"] and tier == "full"`: measured-clean from the full verdict. `source` is used only to balance draws within that already-certified set (and to reject unknown sources). `abc_pass` is not special-cased anywhere in `reprobe.py`.
- **Does `reprobe.py` encode the dropped cheap-first flow? Yes.** `probe_pool` runs the cheap tier on all entries, then `full_tier_entries` (cheap failures + a hash-sampled 10% of cheap passes) runs the full tier. Not changed here; a full-verdict-on-everything flow means `full_tier_entries` should return all non-excluded entries (and `certified` already requires `tier == "full"`, so clean stays correct). Also note `build_splits(cheap_failed=...)` tops clean up from files the cheap tier failed; moot if cheap is dropped.

## Commands and results (all on the VM; exit codes are pytest's own, read from a file, not through a pipe)
- Pool unit file, new pool, native image: 11 passed, PYTEST_EXIT=0. Old pool + new tests: 2 failed, 9 passed, PYTEST_EXIT=1. Mutations: see above, each PYTEST_EXIT=1.
- Final head, validation image: `test_v4_reprobe.py` 92 passed PYTEST_EXIT=0; `test_worker_pool.py` 11 passed PYTEST_EXIT=0.
- `ruff check` on worker_pool.py, kernel_calls.py, reprobe.py, both test files (validation image): `All checks passed!` RUFF_EXIT=0. `mypy` on the three src/devtools files: `Success: no issues found in 3 source files` MYPY_EXIT=0.
- Unit tier, `tests/unit/cad/repair_v4` base vs fin: base 1 failed 608 passed; fin 1 failed 610 passed; same single failure `test_panel.py::test_no_verdict_module_but_panel_spells_the_infrastructure_literal` (pre-existing, not mine). `tests/unit/dev`: base 10 failures (env-bound: settings, fleet launcher, arms launch, wave controls, onboarding, incl. the known `test_the_settings_tier_passes...`); fin 11 = those 10 + `test_v4_reprobe.py::test_splits_disjoint_and_stratified`, which my key change broke and `237891f0` fixed (re-run: 92 passed). `comm -13 base fin`: that one id (now fixed); `comm -23 base fin`: none. The full `tests/unit` tree does not collect in one invocation (conftest name collision, both base and fin), so I ran those two directories, not `make test-fast`.
- Native tier (`meshpipeline-mesh:native`): `tests/native/test_native_repair_v4_*.py` + `test_native_v4_inject_pool.py`, base and fin each `1 failed, 210 passed, 3 skipped`, PYTEST_EXIT=1; the single failure is the known `test_repair_v4_is_callable_from_a_script_without_a_main_guard` in both. `comm -13` and `comm -23` of failure ids: both empty.

## Not done
- Did not change the cheap-first flow in `reprobe.py`; no TVD on the full re-probe; the HXT text rule is only unit-tested, not confirmed on stored real messages; shared-dev verification is out of scope (nothing deployed; this is a pool-protocol and dev-tooling change tested on the VM).
