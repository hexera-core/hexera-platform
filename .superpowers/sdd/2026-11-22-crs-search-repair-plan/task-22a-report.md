# Task 22a report: the engine's entry and exit

Branch `sdd/task-22a`, base `b9d333b3`. Code head `9dc3a92c` (this report is committed on top).
No signature in 04-task-22-engine-seam-contract.md was changed. Nothing pushed, no model, no GCP.

## What was built
`src/meshpipeline/cad/repair_v4/__init__.py`
- `Budget` placeholder deleted; `repair_v4.Budget` is a lazy module `__getattr__` re-export of `search.Budget`
  (lazy keeps the package import stdlib-only). Its fields have no defaults, so
  `conservative._repair_via_v4` now passes `model_calls=0, model_usd=0.0` (one-line change in `repair/conservative.py`).
- `_run_search`: expired deadline -> passthrough, status `not_measured`; `WorkerPool(...)`; `read_step_xcaf`
  (with `brep_out` in a temp dir) -> `Workspace.from_brep(pool, brep, labels)` (labels carried by the caller);
  input verdict `judge.full(pool, start, source)`; the middle `_find_best`; `deliverable(result, input_verdict)`;
  `None` -> `shutil.copyfile(source, dest)`, `delivered=False`; a Workspace -> `write_step_xcaf` (labels, units and
  assembly_ids from the read) to a temp file, then `shutil.move` to `dest` (no partial dest); pool closed in `finally`.
- Unavailability: pool cannot start, XCAF read fails, index fails, write fails, `CheckerUnavailable`,
  `InconclusiveVerdict`, an inconclusive input verdict, `SearchResult.infrastructure_failed` -> all
  `SearchUnavailable` (nothing written, message says "not a finding / never measured").
- `verdict_summary` = `{status: clean|not_clean|improved|not_measured, input: {...}, search: {reason, changed, clean}, delivered: {...} (only when shipped)}`.
  `trace_path` = `<dest>.v4-trace.json`, written when the search returned a trace.

## Seams faked, and what the real thing must provide
- `_find_best(pool, ws, source, *, budget, deadline_s, judge) -> SearchResult` is the ONE seam tests fake.
  Its real body calls the in-tree `run_staged`, `arm_generators(ARM)`, `search(...)` in the frozen order, so 22b/22c
  are reached by real calls once they land. Needs: `SearchResult` with `best`, `best_verdict`, `changed`, `clean`,
  `trace`, `reason`, `infrastructure_failed`; `Verdict` with `clean`, `inconclusive`, `valid.ok`, `panel[].name/.status`, `faithful.ok`.
- `_make_judge()` -> `default_judge(h_mm=H_MM)`; tests fake it. `judge.full` must raise `CheckerUnavailable`/`InconclusiveVerdict` (not a verdict) when infrastructure dies.
- `WorkerPool` faked in the unit tier (real in the native test).

## Not doable without a sibling lane / decisions to route
1. `H_MM = 1.0` is an INVENTED constant (`default_judge` requires `h_mm`; nothing derives it from a part). Owner: 22b/22c or controller.
2. `ARM = "search_det"` constant; `search_ai` needs ledger/route/model key (22c). `budget.model_calls/usd` are passed through to search unchanged.
3. `defects`/`operations` of the delivered `RepairReport` are STILL EMPTY. `V4Result` is frozen and carries neither; `best.history` (Diffs) has the operations but a defect list needs 22c's search result shape (or a new `V4Result` field, which is a contract change). Seam named, not filled.
4. The input verdict is computed by 22a (`judge.full` on the unstaged workspace) because `SearchResult` does not carry the search's own start verdict and staging may change the start; that is one extra full verdict per file (cache-able). 22c could expose the true input verdict to remove the duplicate.
5. `<dest>.v4-trace.json` lands beside `dest` (precedent: hints). If the worker uploads the whole dest directory, tell me.

## Tests
- `tests/unit/cad/repair_v4/test_engine_entry_exit.py` (18 tests, all through `repair_v4()` itself; real `Workspace.from_brep`, real `deliverable`, fake pool/judge/middle). Passthrough test asserts `dest` bytes == `source` bytes.
- `tests/native/test_native_repair_v4_engine_entry_exit.py` (2 tests, real kernel, real STEP, `repair_v4()`): six box faces named in; written STEP re-reads to six faces; passthrough byte-identical.
- `test_engine_dispatch.py::test_unwired_search_is_unavailable_not_a_verdict` replaced by `test_an_engine_that_cannot_run_is_unavailable_not_a_verdict` (the engine is wired now; a pool that cannot start must still be RepairUnavailable).

## Mutations (unit file, each restored; original passes 18/18)
| mutation | broke |
|---|---|
| passthrough writes b"" instead of copying | expired_deadline, clean_input_never_replaced, worse_best_not_delivered, passthrough_byte_identical |
| labels `{}` into from_brep | delivered_writes_..._labels, in_reads_xcaf_and_hands_labels |
| `infra = ()` (checker errors not converted) | 4x dead_checker params, inconclusive_input, search_measured_nothing |
| skip `deliverable` gate (`best = result.best`) | clean_input_never_replaced, passthrough_byte_identical, worse_best_not_delivered |
| `pool.close()` removed | pool_torn_down_on_every_exit, 4x dead_checker params |
| units hard-coded "mm" | delivered_writes_... |
| `infrastructure_failed` check off | search_measured_nothing |
| deadline guard off | expired_deadline |
| `shutil.move` -> copy of source (delivers upload as repair) | delivered_writes_... |

## Id diff, `tests/unit/cad/repair_v4` + `tests/unit/cad/test_repair_conservative.py`, validation image
Base `b9d333b3`: 490 passed, 0 FAILED/ERROR. Head (code commit before the ruff-only test edit): 508 passed, 0 FAILED/ERROR.
`comm -23` (base-only failures): empty. `comm -13` (head-only failures): empty. (Both lists are empty, so the diff is trivially empty.)
After the ruff-only edit the new unit file re-ran: 18 passed.
Native tier (`meshpipeline-mesh:native`): new native file, 2 passed.
ruff on changed src and the two unit files: clean after fixing C416; mypy on `repair_v4/__init__.py` and `repair/conservative.py`: no issues.

## Commands (gate: `git clone --no-hardlinks` to /tmp/clone22a-<ref>, `docker run --rm -v clone:/w -w /w <image> bash -lc 'PYTHONPATH=/w/src python -m pytest ...'`; output redirected to a file, exit code echoed alone)
- unit entry/exit + dispatch: rc=0, 33 passed
- native entry/exit (`meshpipeline-mesh:native`): rc=0, 2 passed
- base full dir: rc=0, 490 passed; head full dir: rc=0, 508 passed
- mutations: each rc=1 as listed above
- lint/mypy run: the container exited 0 only because mypy's tail ended the pipeline; findings read from the log as above (C416 fixed afterwards, then re-run of the unit file rc=0).
- Not run: `make lint/typecheck/check-fast` on the host (no pytest on host python); ruff+mypy were run in the container on the changed files. Native tier ran only my new file.
