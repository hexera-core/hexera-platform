# Lane B11 report: three throttle defects in task 52 (b0c6810c)

Branch `sdd/lane-b` on `f14e5c4d`. Hermetic tests only; no model calls, no VM, no Docker.

## (1) `acquire` could block for ever under a wave back-off. FIXED.
**Cause.** While the wave guard held a call, `deadline += wait` pushed the deadline forward by exactly the time waited, so `remaining` never shrank. `acquire()` then blocked until the trip lifted: 300 s plus jitter per trip, and **unbounded** while new trips kept firing. `proposer.py` calls `acquire` before `_ask`, outside the router's round deadline, so nothing above it bounded the wait either.

**Fix.** The deadline is `clock() + wait_s` and is never extended. Whatever holds the call (this shard's limits or the wave's 429 back-off), `acquire` returns False once `wait_s` has passed.
- The wave hold is still recorded (`guard.note_held`).
- A new counter, `WaveThrottle.waits_timed_out`, goes into the shard's provenance as `prov["model_throttle"]["waits_timed_out"]`.

**Consequence, stated plainly.** A round held longer than `wait_s` (default 120 s) by a wave back-off is now a failed model round, as any other timeout is. Three in a row end the AI for that file as `model_unavailable`. Since B3 that makes the row **inconclusive**, never a failed repair. A wave that stays tripped for many minutes therefore shows up as inconclusive `search_ai` rows plus `waits_timed_out`, instead of shards hanging.

**Red first.** `test_acquire_never_blocks_past_its_wall_time_budget_even_under_a_wave_backoff`, with a guard that always holds 300 s and `wait_s = 5`:
- On the old code it **never returned**. I killed it after 300 s.
- Now it returns False at exactly 5.0 s of the fake clock.

## (2) A malformed trip marker raised `KeyError` into the proposer. FIXED.
**Cause.** `_refresh` stored whatever JSON the marker held, and `hold_for` read `t["until"]` outside any `try`.

**Fix.** `_refresh` validates each marker on its own. It keeps only a JSON object with a finite numeric `until` (bools excluded). Anything else (not JSON, no `until`, a string `until`) is dropped and counted in `malformed_markers`, which is reported in `describe()`. A store read error still fails open once per refresh, as before.

**Red first.** `test_a_malformed_trip_marker_is_ignored_not_raised_into_the_proposer` covers: no `until`, not JSON, `"until": "soon"`, and then a real trip that still holds. It fails on the old code and passes now.

## (3) More shards than in-flight calls floored silently. Now REFUSED unless overridden and recorded.
**Cause.** `for_shard` floored each slice to one call whenever `max_in_flight // n_shards < 1`. A 96-shard wave against the 37-call ceiling therefore ran with up to 96 calls in flight, and nothing refused it. The comment there said "do not fix it back to a refusal"; LEAD-2's B11 instruction supersedes that.

**Fix.**
- `WaveThrottle.for_shard(..., allow_floor=False)` raises `ThrottleConfigError`, naming the ceiling, the shard count and the override.
- `run_arm.py` gains `--allow-in-flight-floor`, which passes `allow_floor=True` and records `prov["model_throttle"]["in_flight_floor_override"]` beside `in_flight_floored`.
- The docstring of `run_arm.build_throttle` (the old line 351) now says what is true: refused unless the override is given, and the override is recorded.

**Who is affected.** Only waves with **more than 37** model-arm shards at the current share. Snapshot v1r runs 6 dev shards and 1 clean shard (37 // 6 = 6), so it is **unaffected**; a test pins the 6-shard slice. A 96-shard dev wave must now be launched with `--allow-in-flight-floor`; its launcher should pass the flag deliberately.

**Red first.** In `test_proposer_wave_resilience.py`, the slice test now asserts the refusal and the override. In `test_v4_arms.py`, a `run_arm` test covers refusal without the flag and the recorded override with it. Both fail on the old code.

## Existing tests changed (each still guards what it guarded, under the new contract)
- **`test_the_throttle_holds_every_new_call_while_the_wave_is_tripped`** used to assert that `acquire(wait_s=5)` *succeeded* after a ~300 s hold, which was bug (1) pinned as intended behaviour. It now asserts that a 5 s budget gives up at 5 s with the hold recorded, and that a call whose budget covers the trip is held until the trip lifts and then goes.
- **`test_a_shard_with_a_ledger_gets_the_wave_guard_and_records_the_floor`** (96 shards) now passes the override explicitly and asserts it is recorded.

## Gates
- **Focused:** `test_proposer_wave_resilience.py`, `test_v4_arms.py`, `test_v4_wave_backoff_record.py`: 111 passed, plus the new override test.
- **Red on base:** the 3 new tests in (2) and (3) fail on the old code; (1) hung on the old code.
- **ruff** on all changed files: clean. **mypy** on `model_throttle.py`: clean.
- **Unit `tests/unit/cad/repair_v4` + `tests/unit/dev`:** base `f14e5c4d` 2180 passed / 39 failed; head 2183 passed / the SAME 39 (host /tmp-path; `comm` empty both ways).
