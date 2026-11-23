# Task 34 report: a wave refuses to launch without a shared verdict cache

Base `cada9133`, branch `sdd/task-34`. Head commit: see `git log -1` (the commit that adds this file).

## Was the two-cache-paths trace right? Yes.
- Wired: `launch_fleet.py` emits `v4-cache-uri` -> `vm_startup.sh:70` `CACHE` -> `:256` `--cache-uri "${CACHE}"` -> `run_arm.run_reprobe` `make_cache(...)`, which reached ONLY `reprobe`.
- Not wired: `search.default_judge` and `stages` read `default_verdict_cache()`, which reads `CRS_V4_VERDICT_CACHE_URI`. Nothing set it from `--cache-uri`. `arms.RealEngine.judge` calls `default_judge(h_mm=...)` with no cache. `run_arms` never read `--cache-uri` at all. So on an arms wave every judge fell back to per-process memory.
- One refinement: `run_reprobe` had `make_cache(...) if args.cache_uri else None`, so a blank flag meant NO cache, not even in-memory.

## The seam
`run_arm.make_cache(uri)` now exports `CRS_V4_VERDICT_CACHE_URI=uri` and returns `default_verdict_cache()`. `main` calls it once, before dispatch, for modes `arms` and `reprobe`; `run_reprobe` uses the same call, so the reprobe cache and the engine's judges are the same object (one cache per wave, one source of truth, asserted with `is`). Env restored in `main`'s `finally` so in-process callers (tests) do not leak it.
Why this seam: `stages.run_staged` takes no cache argument and `default_judge` callers (`RealEngine.judge`, `__init__`, `search.py:692`) do not pass one. Passing the object explicitly would need library signature changes in every caller and would still be forgettable at each one; the env var is the engine's own settings seam, `run_arm` is the sole entry that takes `--cache-uri`, and the cache is bound before any judge exists. (22b's optional `cache` arg is untouched and still works.)

## Refusals
- `wave_spec.shared_cache_problem(uri)`: one definition of "shared" (non-blank `gs://<bucket>/<prefix>`; blank, directory, memory refused). Derives nothing new; `verdict_cache_uri` still the only location definition.
- `preflight.py`: new check `cache_shared` (in `CHECKS`, runs on `loc.cache` derived from `wave_spec`), same Check/Report refusal shape.
- `launch_fleet.v4_cache_problems(metadata)`: checks the `v4-cache-uri` item the VMs are actually handed (exactly one, shared). Called in `main` for v4, a LOCAL check.
- `run_arm.main`: shard-side refusal, exit code 7, reason in PROVENANCE, for modes arms/reprobe with a non-shared `--cache-uri`. Refusal is at the wave boundary only; library in-memory fallback untouched; `cache.py` fail-closed untouched.

## Does a dry run reach it? 
The preflight itself, NO: `wave_preflight` runs only from `live_checks`, which is skipped on `--dry-run` (`not args.dry_run and units`). So the same condition is also checked by `v4_cache_problems` in `main`, which runs on a dry run (`test_a_dry_run_reaches_the_cache_refusal_and_creates_nothing`: rc 1, `REFUSE  verdict cache`). Note the preflight `cache_shared` check is also in the live path via `run_preflight`.
Caveat: with a valid `--prefix` (must be gs://dev-crsv3-...) the derived URI cannot be blank from the CLI today; the check guards the derivation and the emission against regression.

## Hunks in Task 32's files
- `devtools/validation/v3_eval/launch_fleet.py`: (1) new function `v4_cache_problems` inserted just above `live_checks` (+10, ~line 456); (2) 3 lines in `main` just before the "THE BUDGET GATE" comment (~line 813).
- `devtools/validation/v4_eval/run_arm.py`: (1) one `wave_spec_mod = _load(...)` line (line 66); (2) `make_cache` body rewritten + `_PRIOR_ENV`/`restore_cache_env` added (~101-125); (3) `run_reprobe`: one line `cache = make_cache(args.cache_uri)` (~139); (4) `main`: +8 lines before `if args.mode == "arms"` (~460), +1 line `restore_cache_env()` in `finally` (~483).
- `devtools/validation/v3_eval/vm_startup.sh`: NOT touched (it already passes `--cache-uri`).
Also touched: `wave_spec.py`, `preflight.py` and 4 test files. Small diff (155 insertions); no need to sequence after 32, but the `run_arm.main` and `make_cache` hunks are the ones likely to conflict.

## Mutations (run on a clone, restored after)
- Remove `step("cache_shared")` from preflight -> 8 failed in test_v4_preflight (all `test_a_wave_without_a_shared_cache_uri_is_refused[*]` and the always-run test).
- Remove the `v4_cache_problems` call in `main` -> `test_a_dry_run_reaches_the_cache_refusal_and_creates_nothing` fails.
- Disable the run_arm refusal (`if problem:` -> `if False:`) -> `test_a_shard_without_a_shared_cache_refuses_before_any_work[...]` fails.
- Remove the `make_cache(...)` call in `main` -> `test_the_wave_cache_reaches_the_engines_own_judges_and_is_restored` fails.
- Remove the env export in `make_cache` -> same test fails.
(An early M1/M3/M5 attempt used BSD sed and did not apply; they were redone with a python replace, results above.)

## Id diff (tests/unit/dev, tests/unit/cad/repair_v4, tests/unit/settings; container, absolute PYTHONPATH=/w/src, clone at a path with no `v4-`)
- head: 2 failed, 2098 passed, 2 skipped. base `cada9133`: 2 failed, 2077 passed, 2 skipped.
- `comm -23 head base` (head only): EMPTY. `comm -13 head base` (base only): EMPTY.
- The two failures are the known-on-both-sides pair (`test_onboarding_contract...hostile`, `test_configuration_authority...machine_specific_path`).
- The diff proves little: both sides are otherwise zero. What proves the change is the new tests and the mutations above. (A first head run had 8 new failures, all in my new/updated tests; fixed before the final run. Existing arms `main()` tests needed `--cache-uri` because `main` now refuses without one; that is the intended contract change.)

## v3 goldens byte-identical
`pytest tests/unit/dev/test_v4_fleet_launcher.py -k v3` in the container: 7 passed, rc=0 (includes `test_v3_create_command_is_byte_identical_to_the_pre_v4_launcher`, `test_v3_startup_runs_exactly_the_same_commands_as_before`). v3 code paths were not edited.

## Other commands
- ruff check on all changed src+test files: rc 0. mypy on the four changed src files: "no issues" on head and on base (test files have pre-existing mypy noise, not chased).
- Focused tests (arms, launcher, preflight, wave_controls, arms_launch): 240 passed, rc 0.
- No GCP call, no model call, no push, no merge. Only containers I started were stopped (one superseded test run).
