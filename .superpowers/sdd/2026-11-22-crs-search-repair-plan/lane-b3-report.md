# Lane B3 report: search_ai at wave scale

Branch `sdd/lane-b`, rebased on `a43b1d29` (lane C2, Task 50 and Task 51 merged; conflicts were additive and resolved as a union). Live calls: **one** header probe, `curl` to `/v1/responses`, 9 input and 5 output tokens, **$0.0003** (not ledgered: it went around the router on purpose, to read raw headers). `model_ledger.py` is untouched (Task 50).

## Headline
- **The rate limit is not what bounds a 96-shard wave.** Our key is on OpenAI's "Grow" tier: **15,000 RPM and 40M TPM for gpt-6-astra, shared with gpt-5.6-sol and gpt-5.6-terra.** That means production traffic on those two models draws on the same limit. A 96-shard wave can have at most 96 calls in flight, because a shard runs one file at a time. At a 3.3 s round, that is at most about 1,750 RPM even if every shard did nothing but call the model. Realistically it is about 200 RPM.
- **What was wrong, and is fixed:**
  1. A file whose model was never reached was written as a *conclusive* search_ai row. It is now **inconclusive**, with `ai_stop_reason: "model_unavailable"`.
  2. The router ignored Retry-After.
  3. An out-of-quota 429 was retried as if it were a rate limit.
  4. **One hung round could take 605 s, more than the 600 s a whole file is allowed.**
- **New: a per-shard slice of a wave-wide limit (`model_throttle.WaveThrottle`).** It caps in-flight calls, requests per minute and tokens per minute, and adds a cool-down after a 429. A test proves it holds the line against a transport that returns 429s.

## 1. Rate limits

| Source | RPM | TPM | Notes |
|---|---|---|---|
| Response headers, one astra call, 2026-11-23 | **15,000** | **40,000,000** | `x-ratelimit-limit-requests/-tokens`; the remaining counts were full |
| developers.openai.com model page and rate-limits guide | 15,000 / 40M at "Grow" | | Build is 5k/1M and Launch is 10k/4M. Astra, Sol and Terra **share** one limit. Limits are set per organization and per project. |

- The guide says to treat `Retry-After` as a minimum plus jitter, and not to retry `insufficient_quota` or `billing_hard_limit_reached`.
- It does not say whether prompt-cached tokens count toward TPM, so the throttle assumes they do.
- **One gap.** The headers carried no `x-ratelimit-*-project-*` fields. So the 15k/40M is the organization's figure, and a project limit, if one is set, was not visible from here.

## 2. Retry and failure behaviour, as it is now
The stack per proposer round is: AIProposer → router (`call_repair_v4_propose`) → the OpenAI SDK client. Every step below is proven by `tests/unit/cad/repair_v4/test_proposer_wave_resilience.py`. That test uses the **real** SDK and the **real** router over a fake HTTP transport (`httpx.MockTransport`), with sleeps recorded instead of slept.

| Condition | What happens | Bound |
|---|---|---|
| 429 rate limit | The SDK retries twice and waits exactly the `retry-after(-ms)` it was given (if ≤60 s). The router then makes a second attempt after **max(its jittered backoff, Retry-After × (1 + up to 25%))**, Retry-After capped at 60 s. The round fails as `repair_v4_propose:rate_limit`. | **6 requests** per round (2 attempts × 3 SDK tries) |
| 5xx | Same retry path. If one of them succeeds, the round succeeds and **only the final 200's usage is billed** (asserted against the ledger row). | 6 requests |
| Timeout | The SDK retries, then the router does. The round fails as `:timeout`. **New:** an attempt is cut at 120 s and the whole round at 300 s (it was 300 s per attempt with no round cap, so up to 605 s). | ≤300 s per round |
| 429 `insufficient_quota` / `billing_hard_limit_reached` | **New:** classified `INSUFFICIENT_BALANCE`, so the router does not retry it. The SDK still makes its own 3 tries. | 3 requests |
| Any failed round | The ledger reservation is **released, not settled**: the ledger holds billed usage only. The proposer counts one failure and the search goes on deterministic-only. Three in a row stop the AI as **`model_unavailable`** (renamed from `model_failure`). It never raises. | 3 rounds |
| A file where the model never answered | **New:** the row is `inconclusive: true`, with `ai_stop_reason: "model_unavailable"`, so it is excluded from the arm's clean rate. If this happens on more than 10% of files, the report marks the arm NOT MEASURED. This also covers a search that ended before the third failure: AIProposer's new `model_unavailable` property is true when it tried and **no** round was answered. The first version of my test caught exactly that case: the search stopped after 2 failed rounds and the row read as a conclusive AI failure. | |

- **What was verified for jitter:** Retry-After alone would bring a fleet back in lockstep. Twelve identical 429s gave distinct router delays.
- **Mutation check (each mutation restored afterwards):**

  | Mutation | Test that failed |
  |---|---|
  | Router ignores Retry-After | `test_the_router_never_comes_back_sooner_than_a_long_retry_after` |
  | No jitter on Retry-After | `test_jitter_spreads_...` |
  | Quota 429s treated as rate limits | `test_an_out_of_quota_429_is_not_retried...` |
  | No throttle acquire | the throttle bound test and the slot test |
  | Arm uses `stop_reason` only | the new `test_v4_arms` test |

**Known edge, not fixed.** A request that times out on our side after OpenAI has already finished it is billed by OpenAI but not recorded in the ledger. It is rare at 3 s rounds and a 120 s cut. The reservation still bounds the cap, but the true spend is unknown for that call.

## 3. The wave throttle (`src/meshpipeline/cad/repair_v4/model_throttle.py`)
- **A slice, not a shared counter.** This is the same construction as the ledger's cap: each of the N shards gets 1/N of the wave's allowance, so the wave total holds wherever the shards run.
- **The wave's in-flight ceiling** is `0.5 × 15,000 RPM × 3.0 s / 60` = **375 calls**. The 0.5 is because production shares the limit. 3.0 s is the fastest measured round, which is the conservative end for Little's law.
- **Per shard at N = 96:** 3 in flight, 78 RPM, 208k TPM.
- **A wave that cannot be sliced is refused** before any file runs, for example N > the in-flight ceiling (`ThrottleConfigError`).
- **A call is charged its estimated input tokens up front** (the proposer's upper-bound estimate).
- **After a call ends rate-limited**, the shard pauses for Retry-After, or 30 s if none was given.
- **A slot that never comes** within 120 s counts as a failed round, not a hang.
- **Wiring.** `run_arm` builds the throttle after the route probe; `--model-max-in-flight` overrides the derived ceiling. It records `prov["model_throttle"]` and passes the throttle through `arms.run_file` to every proposer. The engine path, `repair_v4()`, stays unthrottled: one file, no wave.
- **The test.** Eight proposers share a 2-slot slice against a transport that answers 429 five times, then 200. All eight get their answer, the provider never sees more than 2 requests at once, and the ledger holds exactly 8 settled calls with nothing left reserved.

## 4. Cost and time projection for search_ai

### Measured inputs
From the hole box: 3 live runs, all clean in 2 turns.
- **Per round:** 4.4k input tokens on the first call, plus about 1.2k per further turn (observation, tool result, reply). 66–106 output tokens, 0 reasoning tokens. 3.0–3.3 s.
- **Per file, 2 turns:** **$0.037 with a warm cache, $0.066–0.069 cold**, as recorded by the ledger. The true cost is about **20% higher**, because the ledger does not price cache writes; that is Task 50's fix.
- **Each further turn** costs about $0.025: 1.2k fresh tokens at $10/M, the same 1.2k written to cache at the +$2.50/M premium, the growing cached history at $1/M, and about 90 output tokens at $50/M.

**Caveat:** the hole box is the easiest case on the benchmark. **Per-file turns on the real dev mix have not been measured.** The scenarios below bracket them, and T2's 10 search_ai files will give the first real number.

### Per file, by turns used (warm cache; first number as recorded by the ledger, second the true cost)

| Scenario | Turns | $/file | Model wall/file |
|---|---|---|---|
| A: like the hole box | 2 | $0.04 / $0.05 | ~7 s |
| B: typical guess | 5 | $0.12 / $0.14 | ~17 s |
| C: uses its whole turn budget (`--max-turns 12`) | 12 | $0.40 / $0.48 | ~40 s |
| Ceiling | – | admission stops at about $0.80 (the $1.00 ceiling minus the $0.205 output reservation) | |

Add about $0.03 per file on a shard's first call if the prefix is cold. With 96 shards starting together, that is about $3 once per wave.

### Per wave

| Wave | Files run on search_ai | A | B | C | Hard cap (ledger) |
|---|---|---|---|---|---|
| Dev (`dev_search`, rotating 100-file subset) | 100 | **$4–5** | **$12–14** | **$40–48** | **$50** (normal wave): binds only near C |
| Checkpoint (`dev_checkpoint`, 300 files) | 300 | **$12–15** | **$36–42** | **$120–144** | $300 |

**Time.** A file's wall is bounded by `SHARED_BUDGET.wall_s` = 600 s per arm, and the model is a small share of it: at most about 40 s, against the 22 s median / 82 s mean for a single full verdict in the re-probe, and a search makes several.
- **Dev, 100 files on 96 shards, 2 arms:** most shards run 1 file and a few run 2. Typical wall is about 5–15 min; the worst case is 2 files × 2 arms × 600 s = **40 min**.
- **Checkpoint, 300 files on 96 shards, 4 arms:** 3–4 files per shard. Typical wall is about 30–60 min. **The worst case is about 4 files × (2 × 600 s for the searches + 2 × ~300 s for the verdict arms) = about 2 h,** inside the 3 h VM backstop but close enough to watch. `evalspec.check_budget` already refuses a projection that outlasts the VM's run duration.

**Rate load at those sizes.**
- **Typical:** about 1 call per 30 s per shard, so about 200 RPM and about 2M TPM for 96 shards: about 1–5% of the limits.
- **Worst theoretical:** every shard calling back to back with 20k-token contexts gives about 1,750 RPM and about 35M TPM. That is over the throttle's 20M token share.
- **A shard's own token slice (208k TPM) starts pacing it** only when it calls back to back with contexts above about 11k tokens. It slows that shard instead of drawing 429s.

## Judgement calls, not implemented
- **The SDK's own retries are nested under the router's**, giving 6 requests per failed round. That is bounded and honours Retry-After, but the router's second attempt mostly repeats what the SDK already did. Setting `max_retries=0` on the client would change every OpenAI role (builder, reviewer, ...), not just this one, so it is left as is.
- **The circuit breaker is per process**, so it cannot open for the wave as a whole: each shard learns the outage separately, after about 3 rounds.
- **WAVE_SHARE = 0.5 of a limit shared with production** is a guess at headroom. If production's Sol/Terra traffic is heavy at wave time, lower it.

## Verification
- **Changed-area suites:** `PYTHONPATH=src pytest tests/unit/cad/repair_v4 tests/unit/dev tests/unit/contracts tests/unit/platform tests/unit/infra` gives **3504 passed, 39 failed** (base `a43b1d29`: 3484 passed, 39 failed; the +20 are the new tests).
- **The 39 failures are the same set as on base `a43b1d29`:** `comm -23 head base` is empty. They are host-environment failures (validation bootstrap requires `/tmp` paths; macOS gives `/var/folders`).
- **`tests/unit/agents`:** the same 2 failures as on base.
- **New tests:** `test_proposer_wave_resilience.py` (17) and 3 in `test_v4_arms.py`. The 5 mutations above each turned at least one of them red.
- **ruff** on every changed file: clean. **mypy** on the changed `src` modules: clean. One mypy error in `arms.py:137` is pre-existing on base.
- **Not run:** the native suite, and any live model call beyond the header probe.
