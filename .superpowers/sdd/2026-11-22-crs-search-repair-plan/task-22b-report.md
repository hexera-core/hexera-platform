# Task 22b report: staged healer + judge wired, one verdict cache

Head: see `git log -1` on `sdd/task-22b` (base `b9d333b3`). Not merged, not pushed. No model call, no GCP call.

## What changed
- `verdict/cache.py`: `MemoryStore`, `default_verdict_cache()` (process-wide; `CRS_V4_VERDICT_CACHE_URI` = `gs://..` or a dir makes it durable), `reset_default_verdict_cache()`.
- `verdict/assemble.py`: a missing STEP while keying the cache is `CheckerUnavailable` (was a raw OSError); a store outage on get/put is a miss/log, never a crash or verdict. `PanelConfigError` (member without `config`) stays loud: no silent uncached run.
- `search.py`: `default_judge(*, h_mm, cache=None)` (optional kwarg only; frozen call shape still valid) now passes the cache (default: the process-wide one) to BOTH tiers. New `h_mm_for(pool, ws)` and `default_judge_for(pool, ws)`.
- `stages.py`: `stages._judge` passes the same default cache (so the staged final state is not meshed again by the search). New `staged_start(pool, ws)` -> `StagedStart(ws, result, measured, reason)`: `CheckerUnavailable` from `run_staged` becomes `measured=False` (input returned, inconclusive), never a failure. `run_staged` signature untouched.
- `stages.step_for` now makes the STEP export deterministic (see finding 1).
- `ws_kernel.py`: new kernel fn `ws_diagonal`.
- Tests: `tests/unit/cad/repair_v4/test_judge_cache_wiring.py` (30), `tests/native/test_native_repair_v4_judge_cache.py` (3), `tests/unit/cad/repair_v4/conftest.py` (fresh default cache per test), `test_search_wiring.py` fixed (its fake pool wrote no STEP file and its members had no `config`: both were fakes that lied about the real contract, and the cache's fail-closed behaviour caught them).

## Findings the controller must route
1. **The STEP export was not deterministic.** OCCT's writer stamps `FILE_NAME` with the wall-clock second (proved: two exports 1.2 s apart differ ONLY on that line). The cache key is the STEP sha256, so a durable (GCS/dir) cache could never hit across processes, and equal geometry at two paths never shared a key. In-process hits worked only through the `step_for` memo. Fixed in `step_for` (header stamp rewritten to epoch; nothing else touched) rather than in `file_sha256`, so reprobe's existing keys are unchanged.
2. **`verdict/mesh_child.py:~242` (Task 3's file, NOT fixed here).** The `message` dict is built eagerly, so `min(qualities)` runs even when there are zero tets and raises `ValueError`; the child exits 1 and the pool reports it as `CheckerUnavailable`. A mesh with no tetrahedra (`no_volume`) therefore reads as infrastructure instead of `fail/no_volume`. I hit it by handing the pool a bare-solid BREP (the xcaf writer exports a bare solid as a shell). Real engine input goes through `read_step_xcaf` (compound root) so it is not on the happy path, but it misclassifies a genuine failure. Route to the Task 3 owner.
3. Entries written under the three-gmsh panel: I DO rely on the config hash changing when Netgen joined the full tier, so they are unreachable misses, not stale hits (test: adding a member changes the key and the old entry is a miss). Correctness is safe; cost is a re-run. No member count is hard-coded anywhere (tests use 1- and 4-member panels of my own).
4. **Input-verdict duplicate (22a item 1): solved by the cache, not by changing `SearchResult`.** Two independent `default_judge` instances share the process cache, so judging the same workspace twice meshes once (test `test_a_second_judge_over_the_same_state_does_not_re_mesh_it`). Caveat for 22a: this removes the duplicate only when the start state IS the input state (staged healer changed nothing, same BREP path/bytes). If staged changed the workspace, the input and the start are different STEPs and the input needs its own one full verdict; that is not a duplicate. 22a should judge the input FIRST with the same default cache.
5. **`H_MM` (22a)**: derived, not invented. `h_mm_for(pool, ws)` = `1000 * fea_plan.base_size(diag_mm / 1000)` (a tenth of the part diagonal, the function production meshes with), diagonal from the new `ws_diagonal` kernel call (no OCCT in the parent). Unmeasurable or zero-extent part raises `CheckerUnavailable`; no fallback constant. Tested at 0.017, 17, 1.7e3 and 1.7e6 mm against literals, and on a real 40x30x20 box natively. 22a should call `default_judge_for(pool, start_ws)` instead of `default_judge(h_mm=H_MM)`. Not asserted: `GmshProd`'s own sizing is only claimed equal via the shared function.

## Seams faked
22a (entry/exit/pool): not implemented, a counting fake `Pool` at the `WorkerPool.call` boundary. 22c (`search`): the REAL `search.search` is used with `generators=[]` for the staged-then-search sharing test; no `arm_generators` involved. 22d: untouched. Layers 1 and 3 are replaced by fakes in the unit tests, real in native.

## Cache-hit evidence (mesher invocations, never time)
- Unit: full tier, 4 counting members: 1st `full` = 4 meshes, 2nd = still 4. Different state = miss (cheap count 1 -> 2). Cheap and full never serve each other. Staged baseline + search start (default cache, no cache handed in): cheap mesher count 1 total, full 4.
- Native, REAL gmsh panel (every member of `default_members("full")` wrapped by a counter): after judge 1 every member `runs == 1`; after judge 2 the list is unchanged; statuses and min_sicn identical.
- Equal geometry at two paths, two exports with different timestamps: 1 mesh.

## Mutations (each run, restored)
- judge ignores cache: 9 unit tests broke (hit, tier, layers, shared-default, durable, no-config, dead-pool, cached-failure).
- stages without cache: 3 broke. Constant config in key: 3 broke (tier separation, shared, no-config). New cache per call: 2 broke. `default_judge` default cache removed: 3 broke.
- `staged_start` stops catching `CheckerUnavailable`: 1. OSError->CheckerUnavailable guard removed: 1. Store-outage guard removed: 1. Cache timeouts/crashes: 1.
- h_mm without mm->m: 7 broke (4 scale params, production-fn, sites handoff, native). h_mm without m->mm: same 7. Size guard removed: 3. Deterministic stamp removed: 3 (2 unit, 1 native). Read-back guard: survived at first; added `test_an_export_that_reports_ok_but_wrote_nothing...`, re-mutated, now breaks 1.

## Failure-id diff (tests/unit/cad/repair_v4, native image, absolute PYTHONPATH, `git clone --no-hardlinks` to /tmp/t22b/{base,br})
- base `b9d333b3`: 460 passed, 0 FAILED/ERROR. branch: 490 passed, 0 FAILED/ERROR.
- `comm -23` (fails on branch, not base): empty. `comm -13` (fails on base, not branch): empty. (Both lists are empty, so the comm is trivial; the +30 are the new tests.)
- Native tier on branch (`tests/native -k repair_v4` + `tests/unit/dev/test_v4_reprobe.py`): 160 passed, 3 skipped, 0 failed. Not run on base (no failures to diff).
- ruff check on `src/meshpipeline/cad/repair_v4`, `tests/unit/cad/repair_v4`, native test: exit 0. mypy on search/stages/ws_kernel/assemble/cache: no issues. Not run: `make check-fast`/`make test-fast` (host venv unavailable); ruff/mypy used the cobalt-sprocket venv binaries.

## Commands and exit codes
Docker runs: `docker run --rm -v <clone>:/w -w /w meshpipeline-mesh:native bash -lc 'PYTHONPATH=/w/src python -m pytest ...'` (exit codes were read with `echo $?` after redirecting to a file): base unit 0; branch unit 0 (490 passed); branch native+reprobe 0 (160 passed); new-tests-only 0 (32 then 30+); mutation runs exited 1 each as listed except the read-back mutation's first run (0, which is how its gap was found). ruff exit 0, mypy exit 0.

## Blocked on a sibling
- 22a: switch `H_MM` to `default_judge_for` / `h_mm_for`; judge the input first with the default cache; use `staged_start` (maps a dead checker to inconclusive passthrough).
- Task 3 owner: `mesh_child.py` eager `min()` (finding 2).
- Not mine, recorded: `Budget`, `ARM`, `defects`/`operations`, trace location.
