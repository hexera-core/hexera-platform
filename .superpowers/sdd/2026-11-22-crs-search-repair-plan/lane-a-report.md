# Lane A report: verdict speed (layers 1 and 3)

Branch `sdd/lane-a`, base `254af0bf`. No model called, no fleet, no deploy. One GCP touchpoint: the existing gate VM
`dev-crsv4-gate-2` (kept alive with `/var/run/gatevm-keep`; nothing created or deleted).

## 1. Profile (10 provisional dev files, native x86, in-process, one check_valid + check_faithful_steps, original == step)

155 s total over 10 files (`devtools/validation/corpora/v4/provisional_sample_task43/PROVISIONAL_dev.json`, first 10):

| step | s | share |
|---|---|---|
| layer 1 shape check of the input | 61.9 | 40% |
| layer 1 shape check of the round-tripped copy | 61.8 | 40% |
|   of which `_self_intersects` (BOPAlgo_ArgumentAnalyzer, both checks) | 104.8 | 68% |
|   `_invalid_entities` / `_overlapping_pairs` / `_non_manifold_edges` / soft | 6.9 / 3.4 / 3.0 / 2.1 | 4/2/2/1% |
| STEP reads: layer 1 input, layer 3 original, layer 3 repaired (same file 3x) | 6.0 + 5.4 + 5.4 | 11% |
| round trip write + re-read | 1.2 + 3.8 | 3% |
| `faithful_check` kernel | 9.3 | 6% |

And the re-probe runs all of it twice: cheap tier, then full tier, on the same unchanged file.

## 2. Changes (verdict only)

- `verdict/memo.py` (new): per-pool memos, `WeakKeyDictionary` on the pool so they die with its workers, keyed by
  (abspath, sha256 of bytes): the STEP read (shared by layer 1 and both layer 3 reads), the `ValidReport` per
  `original_units`, the `FaithfulReport` per declared edits. Successes only; deep-copied in and out; LRU-bounded
  (32 BREPs, 2048 reports per pool).
- `valid.py`: the round trip (write, re-read, check) runs on a second worker alongside the input's shape check.
  Decisions are taken in the old order on the same call results (`_judge_round_trip`). It is started only when the
  new `valid_extent` kernel call (the same bbox rule `check_shape` stops at) says bounded, so an infinite part is
  still never written back; a failed `valid_extent` falls back to the plain order. A raise (file clock,
  CheckerUnavailable) does not wait for the speculative calls; their temp dir is removed when they finish.
- `valid_kernel.py`: registers `valid_extent`.
- `faithful.py`: the process-global, mtime-keyed originals memo is replaced by the shared read memo.
- Tests: new `tests/unit/cad/repair_v4/test_verdict_memo.py` (11); `test_valid.py`'s ScriptedPool now picks check
  results by BREP path, not call order (the checks are concurrent), and answers `valid_extent`.

Not done, on purpose: overlapping layer 3 with the panel (CPU contention could push a mesher past its timeout, and a
timeout IS a verdict); per-solid splitting of the self-intersection check (helps multi-solid files only).

## 3. No verdict change: old vs new on the 30 provisional dev files

`equiv.py` (kept outside the repo, `/Users/kitts/archductor/lane-a-work/`): per file `cheap_verdict` then `verdict`,
original == step, one `WorkerPool(2)` per shard, 7 shards, no verdict cache, no file clock; old tree = `254af0bf`
`src`, new tree = this branch. Compared per file and tier: `reprobe.summarise(v)` as sorted JSON, AND the full
`dataclasses.asdict` of `ValidReport` and `FaithfulReport` (every finding's entities and detail, deviation, ratios).

Result: **summaries identical 60/60, full reports identical 60/60.** No CheckerUnavailable or error row in either run.

## 4. Timings (same runs, native x86 VM, seconds, cheap + full per file)

Layers 1+3 summed over 30 files: **1248 -> 473 s (2.6x)**; median per file **9.6 -> 3.6**; mean 41.6 -> 15.8.
Every file is faster in layers 1+3 (1.7x to 4.0x). Biggest: 537 -> 212, 116 -> 35, 103 -> 46, 101 -> 28, 94 -> 26.
Whole file (cheap+full incl. panel): median 28.6 -> 20.4, mean 151 -> 133, sum 4541 -> 3989. The tail is panel
timeouts (three files at 580-1020 s of panel), which this lane does not touch. Per-file table: `lane-a-work/res/timings.txt`.

Caveat on timing: another lane's `t49-*` containers ran on the same VM during parts of both runs (load avg ~9-11 on 16
vCPU). Panel time, which my change does not touch, was 7% higher in the new run (3293 -> 3516 s), so the layer
speed-up is if anything understated. Not measured: workers=1. There the overlap gives nothing and only the memo helps
(about 2x on cheap+full of one file, from the profile's shares); a raise of `--workers` gives the overlap a worker.

## Commands (exit codes)

- `PYTHONPATH=src task-21/.venv/bin/python -m pytest tests/unit/cad/repair_v4 -q`: base 611 passed; head 622 passed, exit 0.
- `tests/unit/dev`: base and head both 18 failed / 1261 passed, identical failure ids (pre-existing, env-bound).
- Native, host emulated: `docker run meshpipeline-mesh:native ... pytest tests/native/test_native_repair_v4_{valid,faithful,judge_cache,zero_tet}.py`: 56 passed, exit 0.
- ruff check + ruff format --check + mypy on the four src files: clean ("Success: no issues found in 4 source files").
- `make lint` / `make typecheck` / `make check-fast` whole-repo: not run (no project .venv in the worktree).

## Risks

- Memory/disk: up to 32 BREPs per live pool in a temp dir; removed when the pool is collected.
- A speculative round trip that is still running when the verdict raised keeps a worker busy until it finishes.
- Concurrency inside `check_valid` means two layer-1 kernel calls can run at once on a pool with free workers; both
  still have the 300 s call limit, so heavy contention could turn a slow check into CheckerUnavailable (unmeasured,
  never a wrong verdict).
