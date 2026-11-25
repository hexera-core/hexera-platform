# Task 50 - the ledger under-recorded real spend (cache-write premium)

## Headline: before / after on Lane B's actual usage payloads

The seven real `gpt-6-astra` payloads (probe 3, search-run1 2, search-run2 2; `lane-b-evidence/*/wire.jsonl`).

| run | settled before | settled after | gap closed |
|---|---|---|---|
| probe | $0.0319 | $0.0376 | +17.8% |
| search-run1 | $0.0368 | $0.0426 | +15.7% |
| search-run2 (own 2 calls; its ledger.jsonl also repeats run1's rows) | $0.0657 | $0.0789 | +20.1% |
| **total** | **$0.1345** | **$0.1591** | **+18.3%** |

Lane B's "about $0.159" is confirmed (0.1591; rounds to $0.159). The gap closes to the micro-dollar: after the fix a
settled row equals provider usage x rates exactly, asserted by `test_cache_write_tokens_are_settled_above_the_same_usage_without_them`
and `test_lane_b_live_usage_settles_to_the_corrected_figure` (old 0.1345 / new 0.1591, both asserted).

**The 16-20% figure is right as a per-file range (15.7-20.1%, aggregate 18.3%) but it is workload dependent**, not a constant: it is
(write tokens x $2.50/M) / spend, so it is highest on cold first calls (run2 call 1: 4057 of 4060 input tokens were writes) and
lower once the prefix is cached. Ceiling if every input token were a 30m write and output negligible: +25%; unattributed or
12h writes would be up to +150% of input cost. **Effect on the caps** (true spend when the ledger says the cap is reached):
$50 normal -> $57.9-60.1; $300 checkpoint -> $347-360 (not the $350-375 in the brief); $250 held-out -> $289-300. Per-file
$1.00 ceilings are unaffected in kind (the reservation bound holds per call) but each file's true spend sat 16-20% above its ledger figure.

## Head commit
See `git log -1` of branch `sdd/task-50` (base f0f901a3). Three commits; head is the last of them.

## Where the field was dropped (verified, not taken from Lane B)
`adapters/model_inference/protocols/responses_normalize.py::usage_from` (called by `protocols/responses.py:86`, and again in `normalize`) read only
`input_tokens`, `input_tokens_details.cached_tokens`, `output_tokens`. Cost was then computed in two places, both through
`pricing.estimate_cost`: `router._cost_of` (telemetry, `execute(cost_of=...)`) and `router.call_repair_v4_propose` (the figure the proposer
settles). `WaveLedger.settle` takes a ready `cost_usd` and never prices anything, so the ledger was only as right as `estimate_cost`.
Note the provider reports cached AND write tokens as subsets of `input_tokens` (3469 = 2338 + 1128 + 3), so fresh input must subtract both.

Chain now: `usage_from` -> `routing.Usage(cache_write_tokens, cache_write_30m_tokens)` -> `ModelRoundResult` (same two fields) ->
`estimate_cost(..., cache_write_tokens, cache_write_30m_tokens)` in both router sites -> `AIProposer` passes them to `WaveLedger.settle`,
which records `cw` / `cw30` in the row and returns them from `entries()` (old rows read as 0).

## Rates and sources
Source for all astra figures: standard-tier table of developers.openai.com/api/docs/pricing, fetched with curl on 2026-11-23 (no model called).
| model | 30m write | 12h write | status |
|---|---|---|---|
| openai:gpt-6-astra | $12.50/M | $25.00/M | on the page (input $10, cached $1, output $50 match the repo row) |
| openai:gpt-5.6-sol / terra / luna | - | - | NOT DISCOVERABLE: the live page no longer lists any gpt-5.6-* model (it lists gpt-6.1-terra at $0.50 etc.) so these rows cannot even be re-verified. Left unpriced-but-flagged. |

Unpriced handling (`pricing._CACHE_WRITE_UNPRICED`, `pricing.cache_write_unpriced()`): write tokens on those models bill at the input rate (the old
under-count) with a one-time WARNING naming the model; no ratio is inferred from astra. I did NOT make it refuse the call: terra is the second route in
`REPAIR_V4_PROPOSE_ROUTES`, and refusing would remove the fallback. Owner decision: confirm those rates (or retire the models) and add to `_CACHE_WRITE_PRICES`.
TTL not attributed by the provider is billed at the 12h rate (can only over-count). `MODEL_PRICE_OVERRIDES` still takes three prices; write rates
are table-only (no override syntax added).

## One policy-adjacent change, flagged
`router.repair_v4_prices` now returns astra's fresh-input rate as max(input, 30m write) = $12.50, so the admission reservation stays an upper bound
for tokens that are written to cache (previously a lower bound for exactly those tokens). Effect: reservation +~$0.01 per call (astra). Terra unchanged.
Not covered: 12h writes ($25) in the reservation - requests do not set a retention, and all 7 live payloads were 30m.

## Output reservation: what it costs in admissions (reported, NOT changed)
Reservation per call = 4096 x $50/M = $0.205 output + ~$0.07 input with the growth floor (run1: $0.264 then $0.276). Real outputs 68-106 tokens
(~$0.004). Real search calls cost ~$0.020 each (corrected). A file stops being admitted when settled + reservation > ceiling:
- $0.50 file: ~$0.22 settled -> about 11 calls. With a 512-token reservation (~$0.097 per call) ~$0.40 settled -> about 20 calls (~1.8x).
- $1.00 file: about 36 calls vs about 45 (~1.25x).
The reservation must equal what the router allows (`REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS`), so the honest lever is lowering that limit, which risks
truncation (`truncated_rounds` already counts it) - n=7 calls is not enough to pick a number. Left to the owner. (Lane B quoted ~$0.30 spent at the $0.50 cap; I get ~$0.22 once the input share and growth floor are included - same order.)

## What the invoice comparison would need
The org Costs API (`GET /v1/organization/costs`, bucketed by day, group_by project/line_item) needs an org admin key carrying `api.usage.read`; the current key returns 403.
Compare the sum of ledger `entries()` for the project/day with the day's cost for the key's project; line items would show cache-write separately.
Buckets are daily and lag, so it needs a quiet day (or one run isolated in its own project). No credentials were sought.

## Mutations (each applied in a clone, suite run, restored)
Suite: test_model_route, test_model_ledger, test_responses_normalize, test_fail_closed_pricing (85 tests).
| mutation | tests that broke |
|---|---|
| M1 `usage_from` drops cache_write_tokens | 3: settled_above_same_usage_without_them, unattributed_12h, normalisation_with_ttl |
| M2 `estimate_cost` prices writes at input rate | 5 (incl. lane_b_live_usage, own_rate, not_billed_twice) |
| M3 proposer does not pass fields to settle | 1: settled_above_same_usage_without_them (row assertion) |
| M4 router settle-path cost ignores writes | 2 |
| M5 reservation ignores write rate | 1: reservation_prices_input_at_dearest_rate |
| M6 write tokens double billed (not subtracted from fresh) | 6 |
| M7 unattributed TTL billed at 30m | 2 |
| M8 ledger row drops `cw` | 2 |
| M9 terra removed from unpriced flag | 1 |
The headline guard the brief asked for (M1) fails 2 of the settle tests plus the normalisation test.

## Tests added
test_model_route (settled > without; exact usage x rates; unattributed TTL; Lane B figures; reservation rate), test_responses_normalize (2),
test_fail_closed_pricing (3), test_model_ledger (1). Updated: `test_model_round_normalization` field-surface list (two new fields), `test_proposer` astra `_prices`.

## Commands and real exit codes
(all containers: `git clone --no-hardlinks` to /tmp/t50clone, absolute `PYTHONPATH=/w/src`; exit code redirected to file, echoed separately)
- pytest cad/repair_v4 + infra + settings + platform (first run): exit 1, 14 failed / 1769 passed. 10 = round-surface list (fixed), Lane B 0.1592 vs 0.1591 rounding (fixed), proposer astra price (fixed, intended), test_progress_not_age (timing flake under load: passes on rerun), test_configuration_authority::test_no_machine_specific_path_reaches_the_template (pre-existing: fails identically on f0f901a3, exit 1).
- targeted rerun of the touched + the two odd tests: exit 1, 1 failed / 294 passed (only test_configuration_authority). Same test on base clone f0f901a3: exit 1, 1 failed / 82 passed.
- ruff on changed files: found 1 (C408 dict() in a new test), fixed; mypy on the 7 changed src files: exit 0 "no issues".
- mutation run: exit 0 (script), results above.
- tests/unit/dev: see the addendum below.

### Addendum: tests/unit/dev (full, in container, absolute PYTHONPATH)
exit 1: 1 failed / 1278 passed / 2 skipped (853 s). The one failure, `test_onboarding_contract::test_the_settings_tier_passes_with_every_required_setting_hostile`, is the pre-existing settings test (re-run alone on base f0f901a3: see the base exit code in the final message; it fails there too).
