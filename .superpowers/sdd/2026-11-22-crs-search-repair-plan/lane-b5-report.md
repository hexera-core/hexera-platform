# Lane B5 report: the staged regression on the clean control, and the unmetered route probe

Branch `sdd/lane-b`, rebased on `5dc953ea` (Task 53 and the .pyc untracking landed during this task; the `StagedResult` fields and the end of `test_stages.py` conflicted additively and were merged as a union) (it includes Task 49's smoke at `9a76f170`). No model spend, no fleet, no gate VM. The native work ran locally in `meshpipeline-mesh:native` (amd64, emulated).

## 1. The regression, reproduced and explained
File: `00614313_760137e15b9b924e8339e428_step_003.step` (sha256 `a3dc0dd4…39d3`), clean control. I ran the staged healer and took a **full** verdict after every stage that changed the part (`~/.lane-b/b5/repro.py`, native).

| Stage | Cheap score (layer 1 + gmsh_default) | Full panel (prod / default / alt / netgen) |
|---|---|---|
| input | – | pass / pass / pass / pass, clean |
| normalise, shells, solids (kept) | unchanged, default SICN 0.0436 | 4/4, clean |
| edges, faces, bodies, simplify | skipped (no change) | – |
| **condition (kept)** | **rose: default SICN 0.0436 → 0.0911** | **gmsh_prod FAIL** ("HXT 3D mesh failed"), others pass |
| finalise (kept) | unchanged | prod FAIL, 3/4 |

**Which stage did it:** `condition`, which runs split_angle(90°), split_closed and split_continuity(order 1) over every face. The splits leave a part that gmsh's production settings (HXT 3D) cannot volume-mesh. They also improve gmsh_default's worst element.

**Why the transaction did not catch it:** each stage is kept when `score(cheap_verdict(after)) >= score(before)`. The cheap verdict meshes **gmsh_default only**, and on that member this stage was an *improvement*. The other three members are never consulted by the transaction, so a stage that trades gmsh_prod for gmsh_default looks like a win.

**Why the smoke recorded the damage:** the engine (`repair_v4()` on the staged arm) already passes a clean input through its delivery gate (`deliverable`), so it would never have shipped this. But the eval's staged arm (`arms.run_file`) recorded `full(staged.ws)` with **no gate**, and the search arms started their search from the damaged state.

## 2. The fix
- **`stages.confirm_staged(pool, input_ws, result, *, full, input_verdict)`.** It meshes the staged result with the **full** panel. If that result `regressed` against the input (fewer panel members pass, or the input was valid or clean and the result is not), it drops the newest kept stage and meshes the state before it, and so on back to the input. `run_staged` now records `standing`, the state each kept stage left behind. `result.ws` is set to what stands, and `result.confirmation` says `fell_back_to`, `dropped` and `checked`. An inconclusive full verdict raises `InconclusiveVerdict`, because infrastructure is never a verdict on the part.
- **Engine.** `repair_v4._verdict_only` (the staged arm) confirms before its verdict, and the trace records `staged_confirmation`. `repair_v4._find_best` (the search arms) confirms before the search starts, so **the search never starts from a state the full panel says is worse** (new `input_verdict=` keyword, passed by `_run_search`).
- **Eval.**
  - The staged arm records what the **engine ships**: the confirmed state, then `deliverable`, so a clean input is never replaced (`arms._confirmed_or_input`).
  - The search arms start from the confirmed state.
  - The row's `triage.stages.full_panel` records `{fell_back_to, dropped}`.
- **`deliverable()` on the staged path.** It already applied in the engine (`_run_search` → `deliverable(result, input_verdict)` for every arm; covered by `test_staged_that_is_worse_than_the_input_is_not_delivered`). It now also applies in the eval's staged arm, which was the gap.

**Verified on the real file (native, `~/.lane-b/b5/fixcheck.py`):**
- the cheap tier kept `[baseline, normalise, shells, solids, condition, finalise]`;
- the confirmation meshed `finalise → condition → solids` and **fell back to `solids`**, dropping `condition` and `finalise`;
- the result is **4/4 pass, clean**;
- 149 s emulated, the staged run included.

**Cost.** One extra full verdict per file whenever the healer changed something, plus one per stage it has to walk back (here 3; the verdict cache dedups the rest). I chose a walk-back over re-running the stages after a culprit without it: re-running costs a full staged pass per culprit, and a later stage's effect depends on the earlier ones anyway.

## 3. The sixth ledger gap: route probes are now metered
- **`model_route.probe_route_chain(routes, *, ledger)`.** The ledger is now **required**, so there is no unmetered door. Every probe call is admitted before it is sent and settled with the router's priced reply after, under file id `_route_probe` and kind `route_probe`, with cache-write tokens included. A probe that failed unbilled is released.
- **A probe the ledger has no room for is not sent.** It counts as that route failing its probe, so a shard whose slice cannot afford a probe refuses with `NoUsableRoute` before any file runs.
- **The probe's ceiling is $1.00 per shard**, the same as `FILE_USD_CEILING`. A probe reserves its full 4096-token output allowance (about $0.225 on astra) and settles at about $0.002–0.015.
- **Wiring.** `run_arm._prepare_model_route` passes the shard's ledger. The generic helper is `model_route.metered_call(ledger, file_id, ceiling, *, kind)`.
- **Tests that changed because the probe is now on the books:**
  - "every model request was admitted" now counts **every** request on the wire, the probe included. The ledger's total must equal the row's model spend plus the probe's.
  - The exhausted-ledger test now leaves room for the probe and none for a round.
  - A new test: no room for the probe refuses the shard, with nothing sent.

## Tests
- **New, all red without the fix (mutation-checked: each fix removed turns its tests red):**
  - `test_stages.py`, 5: the smoke's case in miniature (cheap tier keeps `condition`; the full panel drops `condition` and `finalise`); everything regressed means the input stands; a confirmed result is meshed once; validity and cleanliness count, not only passes; an unchanged result is not meshed.
  - `test_engine_arms.py`, 2: neither search arm starts from a damaged staged state.
  - `test_v4_arms.py`, 4: the staged arm does not ship the regression; a clean input is never replaced; the search starts from the confirmed state; no probe room refuses the shard.
  - `test_model_route.py`, 3: every probe settles in the ledger; a probe with no room is never sent; no ledger is a `TypeError`.
- **Suites:** `PYTHONPATH=src pytest tests/unit/cad/repair_v4 tests/unit/dev` gives **2022 passed, 39 failed**.
  - **The 39 are the same set** as at B4's head `8fae5545` in those directories (`comm -23` is empty), and that set was identical to its own base.
  - **Scope is narrower than B4's.** A fresh head-vs-base run of the six-directory set was **killed twice at its time limit**: this host's load average was ~350, with another lane's Docker VM at 311% CPU. So I ran the two directories where every changed module and its consumers live, and compared them with the last known-equal set.
  - **Not re-run this round:** `tests/unit/contracts`, `platform`, `infra` and `settings`. Nothing in them is touched except through `model_route`, which `test_model_route.py` (in `repair_v4`) covers.
- ruff on every changed file: clean. mypy on `stages.py`, `__init__.py`, `model_route.py`: clean.
- **Not run:** the native suite as a whole. The real-file check above is the native evidence. One native test fake (`test_native_repair_v4_engine_entry_exit.py`'s `find_best`) was given the new keyword but not run.

## For the lead
- The `.pyc` files Task 49 committed were untracked upstream (`5dc953ea`) while this ran. I had restored them after every local test run and staged my files by name, so none are in this commit.
- **Same blind spot in the search's own ranking:** it uses the cheap tier and confirms only the top K with the full panel, so it has this blind spot within a search too. Its delivery is gated by `deliverable` on the full verdict, so it cannot ship such a state, but it can waste ops climbing toward one.
