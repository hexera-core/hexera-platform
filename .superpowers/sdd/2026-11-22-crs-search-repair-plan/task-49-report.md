# Task 49 report: T2 smoke, THREE-ARM result, `search_ai` PENDING (halted by ruling)

Everything below is **PROVISIONAL**: 30 dev files and 8 clean files drawn from Task 43's 86-file sample, not the full re-probe. Not benchmark numbers.

## Headline
1. **`search_ai` was not run.** The coordinator halted it (ledger under-records spend 16-20%, Task 50). Its container was stopped before its first file finished: ledger `ai10b.jsonl` holds **0 settled calls, 0 reservations**. Metered model spend **$0.0000**. There is **no AI marginal and no model contribution to report**.
2. **`search_det` added nothing on this panel.** 0 files repaired by search over 28 measured files; 26 of 28 ended `exhausted` (the deterministic generator proposed nothing applicable), 15 verdict candidates in total. Its clean count (1) is **a lower bound** (Lane C bug: the search discards the healer's gain when it adds nothing; fixed in task 51). Footprint measured: exactly 1 file (below).
3. **The official dev report REFUSED (exit 1)** and I did not work around it. One file's input verdict disagrees with the split. That is a finding about verdict stability (below).
4. **`staged` damaged a clean file** in the clean control (clean to unclean, 4 -> 3 panel passes).
5. **The 900 s wall fired 5 times and nothing hung; the next file completed normally every time** (Task 48's fix on real data).

## What ran, and on what
- Code measured: **`1b570592`** = base `f0f901a3` (Lane B merge) + my 2 harness commits. **Not** `5c32a632` as briefed: with no paid call made yet I rebased onto `f0f901a3` per the coordinator's option 1 (fixed prompt, 13 of 19 ops described), then the AI arm was halted anyway. `run_arm.py` flags added (only ever LOWER a limit): `--ops`, `--model-usd-cap`, `--file-usd-cap`, `--h-mm auto` (per-part `h_mm_for`; one fixed size is wrong from 10 mm to 10 m parts); `WaveLedger.cap_to`; two WaveDefs `t2_smoke_dev` / `t2_smoke_clean` (input_only, staged, search_det) because a wave must have a row for every (file, arm). Branch `sdd/task-49`; head commit is in `git log -1`.
- Images: mesh `sha256:633e1c3bfd8e2c0fb97b166fa28ff834d0433b84ca8e77c24eb4b00c83ece361`; validation `sha256:690e85c97afcb721e94bb76cdc1aa8e6551cb105a2a71aaaadb01b3649215e99`. VM `dev-crsv4-gate-2`, c2d-standard-16, native x86, `us-central1-b`.
- `ops=24` (`--ops 24`), `--h-mm auto`, `--workers 1`, file wall 900 s, shared cache `gs://dev-crsv3-transfer-224734058693/eval/t49-smoke/verdict-cache`. Dev run as 5 shards (hash cut, **7/4/11/2/6 files**), clean as 1 shard, all concurrent with each other (and with Lane A's containers on the same VM: timings mildly perturbed, pre-Lane-A-optimisation).
- Inputs: the 38 files from Task 43's bundle, sha256 re-verified (0 mismatches).

## Per-arm table, dev split (30 files, PROVISIONAL)
Computed directly from `rows.json`/`EXCLUDED.json` (the official report refused; see below).

| | input_only | staged | search_det | search_ai |
|---|---|---|---|---|
| conclusive rows | 30 | 28 | 28 | NOT RUN |
| clean (of conclusive) | 1 (3.3%)* | 2 (7.1%)* | 1 (3.6%)*, LOWER BOUND | - |
| repaired (not clean at input, clean at output) | 0 | **1** | **0** | - |
| regressions clean->unclean | 0 | 0 | 0 | - |
| fewer panel passes than input | 0 | 1 (`bug32314_corrupted.stp`, 1->0) | 0 | - |
| panel passes up | 0 | 3 files (0->1, 0->1, 3->4) | 0 | - |
| declined | 0 | 0 | 0 | - |
| out_of_scope | 0 | 0 | 0 | - |
| inconclusive rows | 0 | 0 | 0 | - |
| excluded / not measured (900 s wall) | 0 | 2 | 2 | - |
| model $ | 0 | 0 | 0 | $0.0000 ledger |

\* The 1 clean in `input_only` and `search_det` and 1 of staged's 2 is the **flipped file** below (clean at input in this run, `clean=False` in the split). Real repairs: staged 1, search_det 0.

Declined/out_of_scope are 0 because the split was built with `unbounded`/`no_solids` already excluded and has no declined-stratum kinds; "0" here means none were present, not that none exist.

- **Official reports.** Dev, 30 files: `report.py` exit **1 REFUSED**: `00611852_673cbf4282be88b3374992cb_step_008.step [input_only]: the arm saw an input with clean=True but the dev split says clean=False`. All three arms agree with each other (panel 4/4); only the split disagrees. In the split (Task 43) `gmsh_alt` failed `low_quality` at min SICN 0.0042; in this run it passes. **A near-threshold file's verdict is not stable across runs/code states**, so a clean-split label on such files can flip; this will matter at 10,887 files. Not worked around. Sensitivity (clearly NOT a split): dev minus that file, 29 files, `report.py` exit **2 UNRELIABLE** (constant-outcome warnings: search_det 0/27 clean and indistinguishable from input_only; the warning's own false-positive note applies, and here it is the real result: search_det repaired nothing). Clean 8: `report.py` exit **0 OK**.
- **MIN_PAIRED_FRACTION = 0.80.** Dev 30: paired 27/30 = **90%** (3 files had an arm excluded). 29-file sensitivity: 26/29 = 89.7%. Clean: 8/8 = 100%. Above the floor everywhere.
- **Withholding (10%).** Dev: staged and search_det unmeasured 2/30 = 6.7% and 2/29 = 6.9%: under 10%, not withheld. Clean: 0/8 inconclusive, nothing withheld (the thin-sample case did not fire).

## Clean control (8 files)
input_only 8/8 clean (matches split); search_det 8/8, 0 regressions; **staged 7/8: 1 regression**, `00614313_760137e15b9b924e8339e428_step_003.step`, 4 -> 3 panel passes, clean -> unclean. The deterministic healer can break a clean part.

## The AI marginal
**Not measured. 0 repairs contributed by the model, 0 model calls settled.** The 10-file subset (`PROVISIONAL_dev_ai10.json`, round-robin by family label, lowest sha first) is ready for the rerun after Task 50. Its `search_det` half was also not completed. Per Lane B, expect `search_ai` ending clean to mean "the deterministic sweep repaired it and the AI agreed".

## Money
- Ledger: **$0.0000** (0 settles). True model cost is at least that and **not zero to the cent**: `select_route` (the tool-and-image probe, "cents per shard, not metered against the ledger") ran in each of 2 shard starts (first start excluded all 10 files in 7 s on a permissions error, the second was halted). I cannot price those to the cent; check the provider dashboard. Treat the ledger as a lower bound (Task 50).
- **Cap demonstrated to bind before any model run, through `run_arm`'s own path with real GCS markers** (`cap_check.py`, wave id `t49-capcheck-2`, exit 0): slice $50 -> `--model-usd-cap 5` -> 5.0; `--file-usd-cap 0.5`, `--ops 24`; 12 admissions of $0.40 granted, the 13th refused `wave_cap` at $4.80 spent; a second admit on one file past $0.50 refused `file_ceiling`. Marker probe record was missing, so I ran `marker_probe.py --store gcs:dev-crsv3-transfer-224734058693 --rounds 50` (PASSED, 50/50 overlapping rounds): this wrote `_probe/` objects, a pass record and wave markers `t49-capcheck-1/2`, `t49-ai10`, `t49-ai10b` in the shared bucket. Unit tests for the flags and `cap_to`: 235 passed (VM, exit 0 read from a redirected file), then 118 passed after the rebase.
- Not exercised: a cap cutting a real model call (none was made). The coordinator's point that the 4096-token output reservation (about $0.205/call) cuts a $0.50 file off near $0.30 is untested here.

## ops=24: the question is unanswered
**0 of 28 search_det rows stopped on `ops`** (reasons: exhausted 26, clean 1, wall 1). Only 15 verdict candidates in total: most files fail layers (mesher panel, overlap, etc.) with no applicable deterministic op, so the search ends before any budget matters. So this smoke says nothing about whether 24 is too low; the Task 47 floor (3 holes = 14 ops) still stands untested on real parts.

## Timing (pre-Lane-A, 5-7 containers in parallel plus Lane A's)
| | median | mean | p90 | n |
|---|---|---|---|---|
| `input_only` wall (a verdict, cold cache; the Task 43 comparable) | **21.9 s** | **102.4 s** | 237.3 s | 30 |
| Task 43 (full tier / cheap+full) | 21.9 / 31.9 | 82.4 / 97.9 | 139.6 / 181 | 73 |
| staged (900 s excluded counted at 900) | 99.1 | 239.6 | - | 30 |
| search_det (same convention) | 28.6 | 152.0 | - | 30 |
| all arms per file | 154.2 | 494.0 | 1241.6 | 30 |

Same median as Task 43, heavier tail (mean 102 vs 82-98). `input_only` is one tier; the per-file all-arms figure is not comparable to Task 43's. Clean files: input_only median 3.4 s.

## 900 s wall: the observation requested
- Wall hits (4 excluded rows over 3 files): `staged` on `0052_00528484_..._step_003.step` (abc_fail, panel layer; its `input_only` took 330 s); `staged` and `search_det` on `nist_ftc_07_asme1_ap242-e2.stp`; `search_det` on `occt/bug32314_corrupted.stp`. Plus one more: `search_det` on 0052... stopped on its own `wall` budget at 900.9 s with **0 verdict candidates** and was recorded as a **conclusive failed** row (`inconclusive: false`).
- Timed-out arms are in `EXCLUDED.json` as `file_timeout`, never a row, so the report treats them as unmeasured, **not** as failures. Good.
- **Next call after every wall completed normally**: shard 2 ran 3 more files with plausible walls, a fresh worker appeared right after the wall (pid etime matched), and 0 `KeyError`/`Traceback` in any shard log. Task 48's fix holds on real data.
- Finding: a search that hits its wall having made **no** verdict candidate counts as a measured failure, not unmeasured. At ~7% wall-hit rate that can inflate failing counts.

## Lane C footprint (staged minus search_det)
Clean: staged 2, search_det 1, difference **1 file**, `..14c95972d6221f4629_step_000.step` (staged 3 -> 4 panel, clean; search_det `exhausted`, 0 candidates, delivered the input). That is the bug's whole footprint here. `search_det` is therefore a lower bound by 1 of 30.

## Surprises
- Panel/mesher verdict instability on a threshold file (refused the official report).
- Sharding by hash is lopsided: 7/4/11/2/6 files; shard walls 3199/2335/**6273**/93/2937 s. One shard with two 900 s files ran 105 minutes; on 96 shards over 10,887 files expect the same skew on top of Task 43's heavy tail.
- First run attempt excluded all 10 AI files in 7 s: input files copied from macOS were mode 750 and the container user (uid 1000) could not read them. `chmod -R a+rX` fixed it. Zero spend.
- `--wave-id` forces the ledger flag set; non-model shards omit it (the plan provenance carries it).

## Commands and real exit codes
- worktree create 0; `git rebase f0f901a3` 0; ruff on changed files 0 after one import fix; mypy on changed files: only the pre-existing `extra` annotation error in `arms.py`.
- Unit tests in `meshpipeline-validation:local`, absolute `PYTHONPATH=/w/src`: `pytest=0` (235 passed), after rebase `pytest=0` (118 passed).
- `cap_check.py` exit 0 (CAP BINDS). `marker_probe.py` exit 0 (PASSED).
- Shard containers: dev1..dev5 rc 0 (rows 21/11/31/6/17, excluded 0/1/2/0/1, 86 rows + 4 excluded = 90 = 30 x 3), clean rc 0, ai10 rc 137 (stopped by me with `docker stop`, only my own `t49-*` containers touched).
- `report.py` dev: exit 1 (REFUSED); dev 29-file sensitivity: exit 2; clean: exit 0.
- Not done: `collect.py` (not needed, `report.py` loads shards itself); `search_ai`; `v3` arm. VM left running; `secret.env` shredded; no pkill.

Artifacts: `.superpowers/sdd/2026-11-22-crs-search-repair-plan/task-49/` (`results/` rows, provenance, reports, logs; `cap_check.py`; `run_all.sh`).
