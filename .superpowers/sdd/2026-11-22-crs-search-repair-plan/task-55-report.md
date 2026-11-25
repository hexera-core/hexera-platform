# Task 55 report: raise the walls and report the excluded set as a stratum; prove the bucket's conditional PUT

Branch `sdd/task-55`, worktree `/Users/kitts/archductor/sdd-worktrees/task-55`, based on `de96441c`. Not merged,
not pushed. No model called. No VM created. Both parts done; nothing blocked.

---

## Part 1 — the walls

### 1. The measured distribution, and where it came from

Three sources, all real data on this branch (nothing estimated):

| source | what it gives |
|---|---|
| `devtools/validation/corpora/v4/measure_lane_d_20261123/files.jsonl` | **the primary data**: 59 diagonal-stratified files at commit `5eefb130`, one JSON row each with `wall_s`, `cpu_s`, `size`, `mem_peak`, `throttled_s` and the `excluded` reason in full. 8 shards pinned to 4 cores' HT pairs, `--cpus=1`, cold cache. |
| `.../measure_lane_d_20261123/projection.json` + `.superpowers/.../lane-d-report.md` | the sample summary and the simulated 96/80-shard plans, and the `~8.7% pool-weighted` figure this task exists to fix. |
| `.superpowers/.../task-43-report.md` | an **independent** 86-file sample: 3 of 86 excluded by the 300 s `check_valid` wall, 1 by the 900 s file wall, full-tier max 882.6 s. Same shape, different commit and different machine — so the Lane D tail is not one bad run. |
| `gcloud storage ls -l gs://dev-crsv3-transfer-224734058693/eval/v4-reprobe/inputs/**` | the real sizes of all **10,887** staged pool files (`TOTAL: 10887 objects, 60,113,389,471 bytes`), which is what "pool-weighted by file size" needs. Not committed anywhere; recovered from the bucket for this task. |

**The per-file wall distribution (Lane D, n = 59).** Right-censored at the walls, which is the central honesty
problem of this task: a file killed at 900 s reports "900", never its true length.

| | value |
|---|---|
| all 59 files | median 25.5 s, mean 136 s, p90 619.7 s, max 903.7 s (= a kill) |
| the 53 that **finished** | median 19.7 s, p90 200.4 s, p95 339.8 s, **max 643.4 s** |
| killed by the 900 s file wall | 3 (903.7, 902.9, 902.8 s) |
| killed by the 300 s `check_valid` wall | 2 (whole-file wall 619.7 s and 661.5 s, so ~320 s and ~362 s of each file was spent **outside** `check_valid`) |
| `checker_unavailable` from a **work cap**, not a clock | 1 (`2832 body pairs to test exceeds the cap of 2000`, `valid_kernel.MAX_OVERLAP_PAIRS`) |

**Projected over the pool.** The same model `project_reprobe.py` already uses — `log wall = -1.263266 + 0.359666
log bytes`, R² 0.21, residuals resampled — evaluated as the exact mixture over all 10,887 real sizes × all 59
residuals (642,333 points):

| percentile | p50 | p90 | p95 | p98 | p99 |
|---|---|---|---|---|---|
| projected per-file wall | 29.9 s | 309 s | 604 s | 1,602 s | 3,713 s |

and the survival at candidate walls: P(>300 s) = 10.34%, P(>600) = 5.04%, P(>900) = **3.36%**, P(>1200) = 2.53%,
P(>1500) = 2.10%, P(>1800) = **1.84%**, P(>2400) = 1.49%, P(>3600) = 1.03%.

**Pool-weighted exclusion, reproduced before changing anything.** Post-stratification on file size (5 pool
quintiles; each sample file weighted by its quintile's pool mass over the number of sample files in it) gives
**8.66%**, against Lane D's reported 8.7%. The method reproduces theirs, so the before/after below are on one
footing. The 2% limit collect enforces is a plain count fraction over the real pool, and this estimator targets
exactly that quantity, so the two are comparable.

| exclusion cause | n of 59 | pool-weighted |
|---|---|---|
| file wall (900 s) | 3 | 4.66% |
| `check_valid` call wall (300 s) | 2 | 2.67% |
| `MAX_OVERLAP_PAIRS` work cap (not a clock) | 1 | 1.33% |
| **total** | **6** | **8.66%** |

### 2. The new wall values, and why those numbers

**`check_valid` / one-call wall: 300 s → 900 s.** Now defined once, in
`src/meshpipeline/cad/repair_v4/limits.py` as `CHECKER_CALL_TIMEOUT_S`.

- 900 s is above the **p95 of a whole file's** projected wall (604 s): one layer-1 call may now take as long as
  95% of complete files do.
- It is **not** a percentile of the call's own distribution, and I will not pretend it is: that distribution is
  censored *at the old wall*. Two of 59 files say ">300 s" and nothing more. So the choice is bounded from the
  other side instead: the two victims spent 320 s and 362 s outside `check_valid`, so a file whose `check_valid`
  takes the full 900 s needs a per-file wall of at least 1,262 s to finish at all. 900 is the largest round-ish
  call wall that still leaves the chosen per-file wall (1,800 s) room for that 362 s **and** a second long call.
- Still excludes, projected: ~0.9% pool-weighted (see §3).

**Per-file wall: 900 s → 1800 s.** `devtools/validation/v4_eval/evalspec.py: DEFAULT_FILE_TIMEOUT_S`.

- 1,800 s is the **p98.2** of the projected per-file distribution (p98 = 1,602 s) and 2.8× the slowest file that
  actually finished (643.4 s).
- The binding constraint is the VM, not generosity. Re-running `project_reprobe.py` at each candidate cap over 96
  cost-balanced shards and the real pool sizes:

| file wall | vCPU-h | VM-h median (p90) | wall h median (p90) | $ median (p90) |
|---|---|---|---|---|
| 900 s (today) | 335 | 54.8 (56.6) | 5.18 (5.64) | 20.67 (21.33) |
| 1200 s | 361 | 60.1 (62.1) | 5.79 (6.27) | 22.64 (23.42) |
| 1500 s | 382 | 64.5 (66.8) | 6.29 (6.84) | 24.31 (25.20) |
| **1800 s (chosen)** | **400** | **68.5 (71.6)** | **6.73 (7.42)** | **25.83 (27.01)** |
| 2400 s | 430 | 75.6 (78.7) | 7.57 (**8.37**) | 28.49 (29.66) |
| 3600 s | 475 | 86.9 (91.5) | **8.95** (10.21) | 32.78 (34.49) |

  8.0 h is the `--max-run-hours` the pipeline asks `check_budget` about by default, and a wave whose wall passes
  it has its VMs deleted mid-shard. **1800 s is the largest candidate whose simulated p90 wall still fits 8 h;
  2400 s is the first that does not.** (The RUNBOOK's launch command passes `--max-run-hours 12`, which has more
  room; 8 h is the default I used, stated so it can be argued with.)

- **What the raise costs.** 335 → 400 vCPU-h, 54.8 → 68.5 VM-h, **$20.7 → $25.8** (+25%), wall 5.2 h → 6.7 h.
  Per shard, the worst case a single pathological file can burn doubles: 900 s → 1800 s, i.e. ~7.4% of a 6.7 h
  shard instead of ~4.8% of a 5.2 h one. That is the price of the wider wall and the reason not to go further.
- Side effect: **96 shards are now the safe plan.** At 1800 s the 80-shard simulation is 7.7 h median / 8.5 h p90,
  past 8 h. (`check_budget`'s own coarser ×1.5-skew model is kinder — 7.5 h for 80, 6.3 h for 96 — and refuses
  neither at 8 h; the simulation is the stricter of the two.) Lane D's C2D-quota note therefore becomes a launch
  blocker, not a preference: `dev-crsv4-gate-2` must go before a 96-shard wave.
- `evalspec.FULL_MIN_PER_FILE` is a function of the wall, so it was re-derived from the same sample and model at
  the new cap: 111 s/file → **132.4 s/file** (400 vCPU-h / 10,887 files). Leaving it at 111 would have made every
  projection quietly price the old wall.

**What I did NOT raise, and why.** `verdict/assemble.py: MEMBER_TIMEOUT_S = 300` is out of scope on purpose: a
mesher that overruns it is the file *failing the definition* ("meshes within the normal time limit"), not an
exclusion (`is_infrastructure_failure` requires `error_kind == "infrastructure"`), so raising it would change
verdicts rather than exclusions. `valid_kernel.MAX_OVERLAP_PAIRS = 2000` is the one remaining known exclusion the
walls cannot touch (1 of 59, 1.33% pool-weighted). It is a **work** cap, not a clock; raising it without measuring
the per-pair Boolean cost would likely convert a fast, honest `checker_unavailable` into a slow 900 s timeout —
the same exclusion, more money. **That is a decision for the coordinator, not a side effect of this task.**

### 3. Pool-weighted exclusion, before and after — and whether it clears 2%

| | pool-weighted excluded | clears collect's 2%? |
|---|---|---|
| before (900 s / 300 s), measured | **8.66%** | no |
| after, **lower bound** — every wall victim was merely slow and now finishes | **1.33%** (the body-pair cap alone) | **yes** |
| after, **model point estimate** — lognormal conditional survival | **4.75%** = 2.55% (file wall) + 0.87% (call wall) + 1.33% (cap) | **no** |
| after, upper bound — the walls fix nothing | 8.66% | no |

The point estimate uses the model's conditional survival: P(>1800)/P(>900) = 0.548 of the file-timeout mass and
P(>900)/P(>300) = 0.324 of the call-timeout mass survive. A lognormal tail with this residual spread (R² 0.21)
over-survives, so 4.75% should be read as a pessimistic bound, not a forecast — but **it is above 2% and I am not
going to round it away.**

So, plainly: **the raise probably but not certainly clears the limit, and no wall value clears it under the
pessimistic model** (even 3600 s leaves 3.6%, and it breaks the run-duration budget). If the measured rate comes
in over 2%, `collect` refuses, writes no split, exits 2 — **and that is the correct outcome**, now with a named
stratum attached so the refusal is readable instead of a bare percentage. I added nothing that defaults
`--allow-excluded` and did not change its behaviour; I only strengthened its help text to say it must never be
passed without reading the named stratum first.

### 4. The excluded stratum, following the declined-stratum precedent

The precedent is `report.py`'s `declined_stratum` (`face_delete_large` / `face_delete_inclined`, expected outcome
`correctly_declined`, declared by `inject.KIND_STATUS`, removed from every rate and printed on its own line by
`render_text`). I matched the mechanism rather than inventing one:

- `collect.py: excluded_stratum(merged)` builds the block — `files`, `fraction_of_pool`, `limit`, `over_limit`,
  `by_reason`, `by_source`, **`names`** (name, sha256, source and the full reason for every file), and `walls`
  (read from `evalspec.DEFAULT_FILE_TIMEOUT_S` and `limits.CHECKER_CALL_TIMEOUT_S`, never retyped), with
  `counted_as: "none: not a failure, not clean, in no failing count and no stratum rate"`.
- It is in `report()` as `excluded_stratum`, so it lands in the printed JSON and in `collect-report.json`.
- `describe_excluded_stratum` prints it **on its own line on every run, including when it is empty** — so a reader
  never has to infer that nothing was lost.
- The over-limit refusal now names the walls and points at `report.excluded_stratum.names`.
- `report.py` (the arm wave) uses **the same function**: `WaveResult.excluded` carries the shards' EXCLUDED
  entries, `_excluded_block` calls `_collect.excluded_stratum`, and `render_text` prints a
  `## Excluded stratum (N files, X% of the split; walls ...; by reason: ...)` line beside the declined one. The
  pool limit is dropped there, because a split is not the re-probe pool.
- Nothing about the accounting changed: an excluded file was already inconclusive for every arm, so it is in no
  clean rate, clean gain or regression. The test below proves that rather than asserting it.

### 5. No second copy of either wall

- The one-call wall now lives **only** in `repair_v4/limits.py: CHECKER_CALL_TIMEOUT_S`. `verdict/valid.py`
  re-exports it as `CALL_TIMEOUT_S` (faithful.py already read it from there) and `verdict/memo.py` binds
  `READ_TIMEOUT_S` to it — that file previously held its own `300.0` with the comment "repeated here to avoid a
  cycle", which was the real duplication: `valid.py` imports `memo`, so `memo` could not import back. `limits.py`
  is stdlib-only and imports nothing, so there is no cycle.
- The per-file wall stays in `evalspec.DEFAULT_FILE_TIMEOUT_S`, reached by `--file-timeout-s` (`run_arm`,
  `reprobe`, `harness_argv`). `project_reprobe.py` held a second `900.0` as its `--file-timeout-s` default — it
  now reads `evalspec.DEFAULT_FILE_TIMEOUT_S`, so the projection cannot price a wall the wave does not run.

### 6. Tests added (all three things the task asked for)

- `tests/unit/cad/repair_v4/test_valid.py`
  - `test_the_one_call_wall_has_exactly_one_definition_and_every_reader_reads_it` — `valid.CALL_TIMEOUT_S`,
    `memo.READ_TIMEOUT_S` and `faithful.CALL_TIMEOUT_S` are all `is` the limits constant, and no module but
    `limits.py` may *assign* that number (source-scanned, comments excepted).
  - `test_the_per_file_wall_is_larger_than_one_call_may_take`.
- `tests/unit/dev/test_v4_project_reprobe.py`
  - `test_the_simulated_cap_is_the_waves_own_wall_not_a_second_copy` — intercepts `simulate` and checks the cap it
    is handed is `evalspec.DEFAULT_FILE_TIMEOUT_S`.
- `tests/unit/dev/test_v4_collect_pull.py`
  - `test_the_excluded_stratum_names_every_file_and_counts_it_as_nothing` — named, and no count the splits are cut
    from moves.
  - `test_the_stratum_reports_the_walls_it_read_and_never_retypes_them`.
  - `test_the_stratum_line_is_printed_on_every_run_including_when_it_is_empty`.
  - `test_over_the_limit_the_refusal_names_the_walls_and_points_at_the_stratum` — exit 2, no split, `accepted:
    false`, `over_limit: true`, 6 names.
- `tests/unit/dev/test_v4_report.py`
  - `test_the_excluded_stratum_appears_in_the_report_named_with_the_walls_that_made_it` — JSON block and the
    rendered line.
  - `test_a_wall_excluded_file_is_neither_a_failure_nor_a_regression` — **the same wave built twice**, once with
    two files killed by a wall and once with those files absent, with each row pinned to its original index: every
    arm's `clean_out`, `clean_rate`, `clean_gain`, `regressions` and `conclusive` are identical, and `either == 0`.
    A wall kill moves no number; it only appears in the stratum.

---

## Part 2 — the conditional-PUT probe, on the real wave bucket

**The bucket.** `wave_spec.BUCKET = gs://dev-crsv3-transfer-224734058693` (project `hexera-dev`, number
`224734058693`), and `launch_fleet.MARKER_BUCKET = BUCKET.removeprefix("gs://")` →
**`dev-crsv3-transfer-224734058693`**, backend id `gcs/dev-crsv3-transfer-224734058693`. That is the bucket a
shard is handed by VM metadata (`v4-marker-bucket`) and the one `run_arm.make_marker_store` opens, so it is the
bucket the probe has to prove. Markers sit under the `model-ledger/` prefix.

**What I used.** `devtools/validation/v4_eval/marker_probe.py` already existed and already did the hard part
(sequential create + read-back, two real processes released by a wall-clock barrier, an overlap floor, the pass
record the launcher gates on, and three deliberately broken negative-control stores). I extended it rather than
writing a new probe: it was missing latency, and it left its keys behind. I read `ObjectMarkerStore` first —
`create(key, value) -> bool`, built on `GcsObjectStore.create_object_if_absent` with `if_generation_match=0` and
`PreconditionFailed` → `False`; there is indeed no `create_if_absent`.

**Command and result** (the task-21 venv's interpreter, which has `google-cloud-storage`; ADC = `pranav@hexera.ai`):

```
/Users/kitts/archductor/sdd-worktrees/task-21/.venv/bin/python \
  devtools/validation/v4_eval/marker_probe.py \
  --store gcs:dev-crsv3-transfer-224734058693 --rounds 50 --latency-samples 30 --cleanup
exit 0

PROBE PASSED on gcs/dev-crsv3-transfer-224734058693: 50 race rounds (22 with overlapping calls), sequential ok
  create latency (racing, n=100): median 141.2 ms, p90 166.0, p95 179.8, max 5907.6, mean 254.3
  create latency (uncontended, n=30): median 138.7 ms, p90 158.0, p95 167.3, max 189.9, mean 141.3
  cleanup: deleted 83 of 83 probe key(s)
```

The four results:

| # | asked | measured |
|---|---|---|
| 1 | first `create` of a fresh key returns True | **yes** (`sequential ok`) |
| 2 | second `create` of the same key returns False and does not overwrite | **yes** — `sequential_check` asserts both the `False` and that the object still holds the *first* writer's payload |
| 3 | two concurrent creators produce exactly one True | **yes**, 50/50 rounds, and 22 of them had the two calls' intervals provably overlap (floor is 10). The read-back after each round held the winner's payload, which is what catches a silent overwrite |
| 4 | latency of `create` | **median 138.7 ms, p90 158.0 ms, p95 167.3 ms, max 189.9 ms** over 30 uncontended fresh keys; under contention median 141.2 ms, p90 166 ms, with one 5.9 s outlier in 100 calls (a client retry) |

**What the latency means for the wave.** A shard pays two creates before it may spend anything (the wave marker
and its own shard marker, `WaveLedger.for_shard` → `markers.create`), then one `put` per settled model call. Two
creates ≈ 0.3 s per shard start, ≈ 28 s summed over 96 shards. Not a cost driver. The 5.9 s outlier is the number
to remember if a shard-start timeout is ever set tight.

**Cleanup.** All 83 keys this run created (50 race + 1 sequential + 2 writer records + 30 latency) were deleted;
verified with `gcloud storage ls`. The pass record at `model-ledger/_probe/conditional-create` survives on
purpose — it is what `probe_problem` reads — and is now mine (`run_id p1795473590-9073b6`, `passed: true`,
`rounds: 50`, `backend gcs/dev-crsv3-transfer-224734058693`).

**Left behind, not mine:** 53 objects (~20 kB) from an earlier probe run, `model-ledger/_probe/p1795464417-a5279e/`
(2026-11-23 20:07 UTC, before my change added `--cleanup`). I did **not** delete somebody else's objects. One
command clears them when their owner says so:
`gcloud storage rm -r "gs://dev-crsv3-transfer-224734058693/model-ledger/_probe/p1795464417-a5279e/"`.

**Code added for part 2.**
`marker_probe.py`: `latency_check` (timed fresh-key creates), `_stats` (ms summary), `cleanup` (deletes only this
run's `_probe/<run-id>/` keys, never `PROBE_KEY`, and *reports* a store that cannot delete instead of failing),
`verify` also returns the racing calls' durations, `Report.latency` / `Report.cleaned` and their lines in
`describe()`, and `--latency-samples` / `--cleanup`. `LocalDirStore` gained a matching `delete_object` so the probe
can be exercised and proven able to fail without a bucket.
`model_ledger.py`: `GcsObjectStore.delete_object`, and `ObjectMarkerStore.store` / `.object_key` so the probe does
not have to reach into privates. **`MarkerStore` deliberately still has no `delete`** — the ledger must not be able
to remove a marker it depends on (that is what `LostShardLog` exists to catch); a test pins that.

Probe tests in `tests/unit/dev/test_v4_wave_controls.py`: latency is measured and never decides the verdict (a
40 ms-per-create store still passes, and a fast one measures lower); `--latency-samples 0` skips it cleanly;
cleanup removes exactly this run's keys and leaves only the pass record, which still satisfies `probe_problem`; a
store with no `delete_object` is reported, not failed; and the accessors plus the "`MarkerStore` has no delete"
guarantee.

---


## What the dev tier caught that my targeted runs did not

I first ran only the test files that cover the code I edited, and they were green. The full
`tests/unit/dev` tier then found **two real breakages of my own**, both worth recording:

1. **`evalspec.FULL_MIN_PER_FILE` is load-bearing in five launcher tests.** Re-deriving it from 111 to
   132.4 s/file pushed the whole 53,799-file pool on 96 shards from a 25.9 h to a **30.9 h** projected wall,
   so `max_run_hours=30` — "a sane wave" in `test_the_real_pool_at_the_ceiling_is_refused_and_a_sane_wave_is_not`
   and in three `launch_reprobe` tests — stopped being sane. Those tests are about the GATE, not about 30, so
   the sane wave's cap moved to 36 h with the reason written beside it;
   `test_the_default_minutes_are_task_43s_measured_mean` is renamed
   `..._are_lane_ds_measurement_at_the_current_file_wall` and asserts 132.4/60, with a docstring saying the
   figure is a function of the wall and must be re-projected, not nudged, if the wall moves again.
2. **`import evalspec` in `project_reprobe.py` was an order-dependent landmine.** `v3_eval/run_arm.py`,
   `collect.py`, `launch_fleet.py`, `stage.py` and `clean_control.py` each register *their* evalspec in
   `sys.modules` under the bare name `evalspec`. In a session where any of them loads first, my plain import
   bound v3's module, which has no `DEFAULT_FILE_TIMEOUT_S` — so `project_reprobe` failed at import. It passed
   in isolation and failed in the tier, which is exactly the class of bug a targeted run cannot see. It now
   loads v4's evalspec by PATH under a unique name (registered in `sys.modules` before `exec_module`, because
   `@dataclass` resolves its own module through `sys.modules`), and
   `test_the_projector_loads_v4s_evalspec_even_when_v3_owns_the_bare_name` reproduces the collision and pins
   the fix. The same hazard in my new `test_valid.py` test is fixed the same way.

## Gates — command and exit code

Run with `/Users/kitts/archductor/sdd-worktrees/task-21/.venv/bin/python` (this worktree has no venv; that one
holds the pinned `pytest 9.0.3`, `ruff 0.15.19`, `mypy 2.1.0` and `google-cloud-storage`). Every log was
redirected to a file and grepped, never piped into `tail`.

**I did NOT run the full `pytest tests/unit` tier, because the coordinator told me not to**: this 10-core host
was at load 300-360 with three lanes plus another task each running the full tier, a full-tier run was advancing
a few percent per half hour, and the concurrency was making every lane's gate worthless. Nothing below should be
read as the full tier passing. The coordinator will run the full tier once, centrally, on the merged result on an
x86 gate VM. (Independently, `pytest tests/unit` does not currently collect on this branch at all —
`tests/unit/cad/repair_v4/conftest.py` from `4894733a` shadows `tests/unit/conftest.py` under the bare module
name `conftest`, so five pre-existing modules fail to import and the tier exits 2. The coordinator is fixing that
with an `__init__.py`; I left it alone as instructed.)

The tiers I ran — the ones my change actually touches:

| command | result |
|---|---|
| `PYTHONPATH=src python -m pytest tests/unit/cad/repair_v4 -q -m "not external_fixture"` (the walls: valid, memo, panel, model_ledger) | **724 passed, 0 failed, exit 0** (77.6 s) |
| `PYTHONPATH=src python -m pytest tests/unit/dev -q -m "not external_fixture"` (the eval side: collect, report, reprobe, project_reprobe, wave controls, marker probe) | **18 failed, 1320 passed, 2 skipped, exit 1** — all 18 pre-existing, see below |
| `PYTHONPATH=src python -m ruff check <all 15 changed .py files>` | **exit 0** — `All checks passed!` |
| `PYTHONPATH=src python -m mypy src/.../limits.py src/.../model_ledger.py src/.../verdict/memo.py src/.../verdict/valid.py` | **exit 0** — `Success: no issues found in 4 source files` |
| `PYTHONPATH=src python -m mypy devtools/validation/v4_eval/{collect,evalspec,marker_probe,project_reprobe,report}.py` | **exit 0** — `Success: no issues found in 5 source files` |
| `marker_probe.py --store gcs:dev-crsv3-transfer-224734058693 --rounds 50 --latency-samples 30 --cleanup` | **exit 0**, PROBE PASSED (part 2 above) |

`ruff format --check` **fails at HEAD too and I did not reformat to make it pass.** `make lint` is `ruff check`
only and the tree is not ruff-format formatted. Measured, not assumed: I extracted all 14 files at their
`de96441c` versions into a scratch directory and ran `ruff format --check` there — **13 of 14 would be
reformatted before my change**, and 11 of 14 after it (my edits happened to make `verdict/memo.py` and
`verdict/valid.py` format-clean). So the check is no worse for any file I touched.

`devtools/quality/mypy_ratchet.py` (the repo's own typecheck gate) reports **2 NEW errors, both platform
artefacts of this macOS host and neither in a file I touched**: `src/meshpipeline/cad/fea_joins.py` and
`src/meshpipeline/engines/radius_chords.py`, `Module has no attribute "sched_getaffinity"` — `os.sched_getaffinity`
is Linux-only and the baseline was taken on Linux. Last touched by `5c71a314`, not by me.

### `tests/unit/dev`: 18 failed, 1320 passed, 2 skipped (exit 1) — all 18 pre-existing

Before my fixes above the same tier was **24 failed / 1313 passed**; the 6 I had broken are gone and the
remaining 18 are the host-environment failures this Mac has at HEAD, none of them in a file or module this
task touches:

| file | n | why it fails on this host |
|---|---|---|
| `test_validation_runner_isolation.py` | 15 | `VALIDATION BOOTSTRAP FAILED: the task workspace must be an absolute path inside /tmp` — macOS resolves `/tmp` to `/private/tmp`, and pytest's tmp_path lands under `/var/folders/...` |
| `test_onboarding_contract.py` | 2 | spawns a nested pytest over `tests/unit/settings` in a freshly built venv; fails on this host for the same environment reasons |
| `test_mypy_ratchet_verdict.py` | 1 | the mypy ratchet's own 2 new errors: `os.sched_getaffinity` does not exist on darwin (see above) |

That is consistent with lane B4's report, which recorded 40 host-environment failures over a wider selection of
these directories at a nearby base and showed the same set before and after its change.

### `tests/unit/cad/repair_v4`: 724 passed, 0 failed (exit 0)

Re-run after the `test_valid.py` fix, so this is the state of the tree as committed.

## What I could not do, and what is left for someone else

- **I did not run the full unit tier**, on the coordinator's instruction (see above). Nothing here says it passes.
- **The remaining exclusion may still be over collect's 2% limit.** The projection says 1.3%-4.8%; only the real
  wave measures it. If it lands over 2%, the decision in front of the coordinator is `valid_kernel.MAX_OVERLAP_PAIRS`
  (a work cap, ~1.3% pool-weighted on its own) or an owner ruling on the limit — not `--allow-excluded`, and not
  another wall raise, which the run-duration budget has no room for.
- **The 96-shard plan is now load-bearing**, so Lane D's C2D quota note is a blocker: `dev-crsv4-gate-2`
  (c2d-standard-16, 16 of the region's 100 vCPU) has to be deleted before a 96-shard wave, and that is its
  owner's call, not mine.
- **53 stale probe objects** (~20 kB) from an earlier probe run remain at
  `gs://dev-crsv3-transfer-224734058693/model-ledger/_probe/p1795464417-a5279e/`. Not mine; not deleted.
- **I did not fix the `conftest` shadowing** that stops `pytest tests/unit` collecting, because the coordinator is
  fixing it in their own checkout and asked me not to collide.
- **`devtools/validation/v4_eval/__pycache__/*.pyc` are tracked in git** (committed by accident in `076da4aa`) and
  my runs dirty them. I restored them rather than committing the churn; removing them from the index is a separate
  cleanup nobody has done.
- Not run: the native tier, the integration tier, any wave. No model was called. No VM was created. No deploy.

