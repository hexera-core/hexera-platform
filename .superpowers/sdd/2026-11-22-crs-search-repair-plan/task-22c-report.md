# Task 22c report: dispatchable arms, ledger-attached search_ai, real router under the proposer

Head: see `git log -1` on `sdd/task-22c` (base `b9d333b3`). Not merged, not pushed. No model call, no GCP call, no VM.

## GATE NOT RUN TO COMPLETION - gated by the controller at merge
* Base `b9d333b3`, full `tests/unit/cad/repair_v4 tests/unit/dev`, clean clone, abs PYTHONPATH: 1 failed, 1613 passed, 2 skipped
  (the one failure: `test_onboarding_contract.py::test_the_settings_tier_passes_with_every_required_setting_hostile`).
* Head `8e17f4f1` (before the last fix commit), same invocation: 4 failed, 1661 passed. `comm -13 base head` = 3 ids, all mine:
  `test_v4_arms_launch::test_startup_mounts_a_key_only_...`, `test_v4_preflight::test_the_launcher_parser_defines_every_arg_the_launcher_reads`,
  `test_v4_reprobe::test_run_arm_parser_carries_every_flag_run_reprobe_reads`. `comm -23 base head` = empty.
* Those 3 were fixed in `9b16076e`. The FULL gate on `9b16076e` was started and then killed at the controller's request: I do NOT have a full-suite number for the final head.
* What I did see on `9b16076e` (clean clone, abs PYTHONPATH, exit 0): `test_v4_arms_launch, test_v4_arms, test_v4_preflight, test_v4_reprobe, test_v4_fleet_launcher, test_model_route` -> 220 passed.
* ruff clean on all changed files; mypy clean on `measurement.py`, `model_route.py`, `search.py` (container).
* Not rebased on 22a (`5cd6a5fe`) / 22d (`3eabd457`).

## What a VM needs for search_ai (both, plus a secret the operator names)
1. A key: `OPENAI_API_KEY` in the container env (read by `settings.providers` at import). The v4 VM has none by default. `vm_startup.sh` already mounts `secret.env`
   when metadata `v4-model-secret` is set, but `V4Profile.model_secret` is None, so nothing set it. I added `launch_fleet --model-secret NAME`
   (REQUIRED when an arm uses a model; no default, nothing invented). NOT verified: that the VM service account may read that secret,
   or that v3's `openai-api-key` is the one to use.
2. A router in `run_arm`: was missing. `meshpipeline.cad.repair_v4.model_route.install_router()` now installs the same `CachingModelRouter` over the real router
   plus a `LocalCapacityController`, called from `run_arm._prepare_model_route`. Without the capacity controller the router returns an "overload" marker (mutation M9).
3. `--h-mm` (no default exists anywhere; the search arms are refused without it) and `--split-file`. These are OWNER DECISIONS: the element size, and the splits staged to GCS.
`run_arm` refuses the shard (exit 6, reason in PROVENANCE) before any file if there is no key, no route passes the probe, or there is no ledger.

## Is the ledger consulted on the production path? Yes, three links, each with a failing test
`main` builds the ledger -> `run_arms(args,out,prov,ledger)` -> `arms._search_arm` puts it in `arm_generators("search_ai", ledger=...)` -> `AIProposer._admit` -> `ledger.admit` before `_ask`.
* `test_main_hands_the_ledger_it_builds_to_the_arms` (M1: main passes None -> FAILS)
* `test_search_ai_reaches_the_real_router_with_the_ledger_attached...` spies `ledger.admit`; admit count == model requests on the wire; row `model_usd` == ledger spend (M2: ledger=None -> FAILS)
* `test_an_exhausted_ledger_stops_the_model_before_the_wire...` and `test_model_route::test_a_ledger_with_no_room...`, `..per_file_ceiling_holds_through_the_router` (M4: admit bypassed -> FAILS)
* `test_search_ai_with_no_ledger_is_refused...` / `run_file` refuses a model arm with no ledger (M3: guard removed -> FAILS)
* reprobe and harness modes make no metered call, so they get no ledger (stated in code); only `arms` mode does.
Caveats: the route probe (a few cents per shard) is NOT metered against the ledger. `Budget.model_usd` stays advisory (once per expansion); the $1.00 ceiling is `admit`'s per-call reservation.

## Mutations (each reverted; failing tests named)
M1 main discards ledger -> 2 failed. M2 ledger None in ai kwargs -> 3. M3 no-ledger guard -> 1. M4 admit bypassed -> 3. M5 report keeps its own rule -> 1
(`test_the_report_and_the_search_share_one_definition_of_measured`). M6 no key check -> 1. M7 unmeasured row written conclusive -> 1. M8 shard_argv drops --n-shards -> 5. M9 router not installed -> fails (NoUsableRoute, capacity error).

## gpt-6-astra: verified vs assumed
Verified (fake transport at `providers.client_for`, real router): default `AIProposer` path -> `call_repair_v4_propose` -> `CachingModelRouter` -> router; model `gpt-6-astra`; `max_output_tokens` 4096; tools sent;
priced at 10/50 per Mtok into the ledger; unpriced/failed routes refused or marked; probe falls from astra to terra on failure; rounds are forwarded, never recorded/replayed.
ASSUMED, not verified (no model call allowed): that astra actually honours tools and image inputs. `probe_route` checks this once per shard before any file, and the shard refuses if no route passes, but that probe has never run against the live model.

## ArmRow boundary: touched, in the direction Task 24 wants
New `repair_v4/measurement.py` holds the one rule (`no_verdict_failure`, `file_unmeasured`). `search.py` uses it to set `infrastructure_failed` and gains `SearchResult.unmeasured`;
`report.ArmRow.derived_inconclusive` now imports and calls it (lazy import, as report.py:588 already does). The field NAMES are still mirrored on `ArmRow` (a dataclass cannot import them);
no new `ArmRow` fields. Task 24 should know this moved. Rows written by `arms.run_file` carry `reason/verdict_candidates/inconclusive_candidates/infrastructure_failed`; an unmeasured search is a row with `inconclusive=True`;
a dead checker / timeout / missing file is an EXCLUDED entry, never a row.

## What was faked
`WorkerPool` (no OCCT), `Engine` (22a open/22b run_staged+judge; real `RealEngine` composes the shipped functions), verdicts (real `Verdict`/`score` over test_search_wiring's world), provider client. Real: run_arm, arms, arm_generators, search, AIProposer, ledger, router, pricing.

## Other changes
`evalspec`: `shard_argv` now emits flags run_arm accepts (a test parses every wave's argv through the real parser); refuses a model wave without ledger_path/marker_bucket; `DISPATCHABLE_ARMS` (v3 arm excluded).
`launch_fleet` v4: `--arms --wave --split-uri --h-mm --model-secret`; one unit per shard (`arms_<wave_id>:pool:I/N`) so one ledger per shard; `vm_startup.sh` runs `--mode arms` with the split mounted; reprobe command unchanged.
Not done: the launcher's cost projection still prices the re-probe, not an arms wave. The stub-based startup test cannot assert the secret.env mount (shared `docker ps` stub races the file removal).
Note for 22d: arms dispatch assumes nothing about images (renders live in the search/proposer path).
