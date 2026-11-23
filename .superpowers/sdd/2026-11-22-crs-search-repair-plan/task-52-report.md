# Task 52 report: WAVE_SHARE 0.05, one-call floor, wave-level 429 guard

## Derived ceiling and slice at WAVE_SHARE=0.05, N=96
- `wave_max_in_flight` = floor(0.05 x 15000 x 3 / 60) = **37**. `37 // 96 = 0`, so the arithmetic alone REFUSES the wave
  (confirmed by running `for_shard` on the base: `ThrottleConfigError`). I stopped and reported; the coordinator
  approved **option 1: floor of 1 in-flight call per shard**, share unchanged.
- Now at N=96: **1 in flight per shard (96 wave-wide, 2.6x the derived 37, deliberately)**, 7.8125 RPM and
  20,833 TPM per shard = 750 RPM / 2M TPM for the wave. `Slice.floored=True` and it is written to provenance
  (`model_throttle.in_flight_floored`). The comment at the floor in `model_throttle.py` states why a floor above the
  derived ceiling is safe (ceiling assumes saturation; shards sustain ~118-361 RPM; RPM/TPM slices are the real bound).
- Wave is sliceable and launchable. Refused still: n_shards < 1, route without a measured limit.

## Head commit
`e2d0f604` on `sdd/task-52`, rebased on `bbcbed67` (3 commits). `model_throttle.py` had NOT moved between
`9f3471f6` and `bbcbed67`; `run_arm.py` and `proposer.py` had (I touched run_arm only, rebased cleanly). Not pushed, not merged.
Note: `devtools/validation/v4_eval/__pycache__/*.pyc` are TRACKED in this repo; I accidentally committed rewritten
ones in the first two commits and restored them in the third (net diff has none). Worth untracking separately.

## How the global guard coordinates (WaveBackoffGuard, `model_throttle.py`)
- State lives in the wave bucket's `MarkerStore` (same store the WaveLedger uses; `WaveLedger.markers` read-only property added).
- Fixed 300 s wall-clock buckets B. Keys `<wave>/429/B/slot-0..slot-10` (threshold 10, so 11 slots). A shard whose call
  ended rate-limited claims the first free slot with atomic `create`. The claimant of slot-10 (the 11th, i.e.
  more than 10) writes `<wave>/429/B/tripped` (`until = now + 300 s`). Every shard reads the tripped marker of the
  current and previous bucket at most every 10 s (2 GETs, nothing listed), and while tripped holds every new call
  until `until` + a per-shard 0-30 s resume spread. Only slot writers pay on 429; reads are cheap.
- A wave hold does NOT spend a call's `wait_s` budget (otherwise a trip would make every round "fail" and read as
  `model_unavailable`); it is recorded instead.

## What it does NOT guarantee
- Lag: up to ~10 s read interval (+ the claimant's write) after the threshold is crossed, calls still go out. In-flight calls are not cancelled.
- It counts calls that ENDED rate-limited (after router retries), not raw 429s: trips later than a raw count.
- Fixed buckets, not sliding: a burst split across a boundary (10 + 10) trips neither.
- Wall clock across hosts: skew shifts bucket edges and `until`.
- Fail-open: a marker-store error means no trip is seen or raised; the shard keeps only its own cool-down; counted in `store_errors`
  and the report shows `unrecorded`, never `free`.
- It is a brake, not a limiter; the per-shard RPM/TPM slices bound the untripped rate.
- `insufficient_quota` / `billing_hard_limit_reached` remain `INSUFFICIENT_BALANCE`, un-retried, and are never reported to the guard (test).
- Not exercised against real GCS in this task (no GCP call); the guard only needs the `get`/`create` protocol the ledger already uses.

## How the back-off appears in the report
- Each shard writes `PROVENANCE.wave_backoff` (`describe()`: reported_429, tripped_here, trips_seen, held_s, holds, store_errors), or
  `{"guard":"absent", reason}` when it had no markers or wave id; `model_throttle` provenance adds `in_flight_floored`, `rate_limited_calls`.
- `report.backoff_summary` merges them into `report["throttle"]` with `status`: `throttled` (a trip, or any held time),
  `free` (every shard ran the guard, no trip, no hold), `unrecorded` (a shard without the guard or a store error; absence is not claimed),
  `not_applicable` (no model arm). Text report prints `THROTTLED WAVE ... NOT the engine's wall`, or `back-off NOT RECORDED`, or `none`.
  JSON `numbers_valid` is unchanged (quality numbers are unaffected); the wall is what is flagged.

## Mutations (each run against the 2 test files; restored after; list = tests that failed)
| Mutation | Failed |
|---|---|
| no floor (`wave_in_flight // n`) | test_a_wave_is_sliced...floored_at_one_call; test_a_shard_with_a_ledger_gets_the_wave_guard... |
| claimant never trips (`if True`) | test_429s_across_shards_past_the_threshold_trip...; test_the_throttle_holds_every_new_call...; test_a_429ing_provider_across_shards_trips... |
| slots threshold+3 (first run survived: 7th report not asserted) | after adding "only one shard writes the trip": test_429s_across_shards_past_the_threshold_trip... |
| throttle ignores guard (`wave_hold = 0`) | test_the_throttle_holds_every_new_call... |
| hold spends wait budget (no `deadline += wait`) | test_the_throttle_holds_every_new_call... |
| release never reports | test_a_429ing_provider_across_shards_trips... |
| store error propagates | test_a_marker_store_failure_fails_open... |
| WAVE_SHARE back to 0.5 | ...floored_at_one_call; ...records_the_floor |
| report hides throttle (always "free") | test_a_wave_that_held_calls...; test_a_wave_is_free_only_if... |
| unrecorded -> free | test_a_wave_is_free_only_if... |
| run_arm builds no guard | test_a_shard_with_a_ledger_gets_the_wave_guard... |

## Commands and real exit codes (venv: `sdd-worktrees/task-21/.venv`, PYTHONPATH absolute, PYTHONDONTWRITEBYTECODE=1; codes read from redirected files)
- `pytest test_proposer_wave_resilience.py` (before extra tests): rc=0, 23 passed. Final two files: rc=0 (clean), all pass.
- `ruff check` on the 6 changed files: rc=0. `mypy model_throttle.py model_ledger.py`: rc=0 "no issues in 2 source files".
- `devtools/quality/mypy_ratchet.py`: rc=1, only `fea_joins.py` / `radius_chords.py` `sched_getaffinity` (macOS host, files untouched).
- Full `tests/unit`: collection aborted rc=2 identically at base and head (5 files: `FakePublisher` conftest clash under my PYTHONPATH), and a full run was
  too slow on a loaded host, so I ran `tests/unit/cad` + `tests/unit/dev` (pytest rc=1 both, 2765 s head / 2935 s base):
  head 27 failed / 5769 passed, base (`bbcbed67`) 27 failed / 5760 passed.
- Failure-id diff (sorted `FAILED` ids): `comm -23 head base` (head-only) = EMPTY; `comm -13` (base-only) = EMPTY; common = 27. +9 passes are the new tests.
  I did not run `tests/unit` outside cad/dev, nor the native/container tiers; no deploy, no model call, no GCP call.
