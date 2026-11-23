# Lane B report: AI proposer live readiness (gpt-6-astra)

Branch `sdd/lane-b` from `254af0bf`. Real model calls: 7, all on `openai:gpt-6-astra`, all admitted against a ledger before they were sent. No fleet, no VM, no deploy, no merge.

## Headline
**astra works on the real route. It made tool calls, read the image, hit the prompt cache, and repaired the fixable hole box clean in 2 turns.** The fallback to `gpt-5.6-terra` was not needed.
**One ledger defect blocks trusting the cap: OpenAI bills cache WRITES, and the ledger prices them as plain input.** On these runs that under-recorded spend by 16 to 20%. Details are in section 1.
**On this case the AI did not prove it adds anything.** It proposed exactly the det sweep's two ops, and the det candidates were evaluated first and won.

Total lane spend, by the ledger: **$0.1345**. Corrected for the cache-write premium: **about $0.159**. Lane cap: $2.

## 1. Live probe, and ledger price against billed usage
Command: `live_readiness.py probe --spend` on the host, with the real router (`model_route.install_router`) and the real key from Secret Manager `openai-api-key` (hexera-dev).
- `probe_route` sent one 16x16 red PNG and forced `report_colour`. **The model called the tool with `{"colour":"red"}`**: 86 input tokens, 18 output, $0.00176.
- **Prices.** The ledger rates for astra are ($10 in, $50 out, $1 cached per 1M). They match developers.openai.com/api/docs/pricing as read today, and terra's ($2, $12, $0.20) match too.
- **Arithmetic.** Each settled row equals provider usage × those rates to the micro-dollar (3/3 probe calls; recomputed from `ledger.jsonl`).
- **Not verifiable from here.** The org Costs API refuses this key: HTTP 403, `Missing scopes: api.usage.read`. "Billed" therefore means the provider's own `usage` block, not an invoice.
- **DEFECT (not fixed; it spans shared pricing code).** The raw usage carries `input_tokens_details.cache_write_tokens` (with `ttl_30m_tokens`/`ttl_12h_tokens`). The pricing page lists a **30m write at $12.50/1M and a 12h write at $25.00/1M for astra**. The normaliser (`responses_normalize.usage_from`) drops the field, and `pricing.estimate_cost` prices those tokens as fresh input at $10.
  - Writes are about 1.1–4k tokens per call here: each call writes its new suffix.
  - Under-billing: run 1 by $0.0058 of $0.0368 (16%); run 2 by $0.0132 of $0.0657 (20%).
  - **Fix:** carry `cache_write_tokens` (by TTL) through `Usage`/`ModelRoundResult`; add write prices to `_PRICES`, or a parallel table, for the OpenAI rows; price them in `estimate_cost`; record them in `WaveLedger.settle`.
  - The per-call reservation still bounds each call (it prices all input uncached and reserves 4096 output tokens), but **the settled totals that the wave cap sums are low**.

## 2. Prompt caching
Two calls on the proposer's REAL static prefix (system prompt + tool schemas, same `prompt_cache_key`):
- **call 1: 2370 input, 0 cached, 2244 cache-write; call 2: 2370 input, 2215 cached ($0.0251 → $0.0051).**
- In the search runs, the prefix and history are served from cache on each next turn: 2338/3469 and 3466/4653 cached in run 1.
- The prefix is byte-stable per stage list, as designed.

## 3. search_ai on the fixable injected case (3x3 mm flush square deleted from a 100x60x40 box)
Command: `live_readiness.py search --spend --usd 0.50 --turns 6 --ops 12 --wall-s 600`, run in `meshpipeline-mesh:native` (amd64 emulated on arm64). It goes through the production `repair_v4()` with `ARM=search_ai`, `ROUTE=openai:gpt-6-astra`. Only `_ai_kwargs` is wrapped, so the ledger is a persistent `for_shard` log instead of `for_file`; nothing in the engine is faked.

| run | prompt | result | turns | model $ (ledger) | wall | path |
|---|---|---|---|---|---|---|
| 1 | base (`254af0bf`) | **clean, delivered**, 4/4 panel pass, faithful | 2 | $0.0368 | 88 s | S0 -fill_loop-> S1 -make_volume-> S3 |
| 2 | + op summaries | **clean, delivered** | 2 | $0.0657 (prefix changed → cache miss) | 109 s | same |

- The model's turns, from the transcript:
  - **Turn 1:** `fill_loop` on E21–E24, G0. Its rationale: "a missing patch rather than an intentional through-hole", which is the intent call §5.8 assigns it.
  - **Turn 2:** `make_volume`, after seeing the open-shell error cleared.
- No malformed call, no truncation, 0 reasoning tokens. Default effort was `medium`, because nothing sets one.
- **Attribution.** In both runs the winning children came from the `det` candidates, and the AI's identical candidates were evaluated after them as duplicates. The case proves the live loop works end to end. **It does not prove AI marginal value.** An AI-only ablation (det generator removed, about $0.07) is the cheapest next check; it is not done here because it is outside the brief.
- Transcripts, images elided: `lane-b-evidence/search-run{1,2}/transcript.json`. Each folder also holds `trace.json`, `wire.jsonl` (every request plus the raw response with usage) and `ledger.jsonl`. Probe evidence is in `lane-b-evidence/probe/`.

## 4. System prompt and tool schemas against design §5.8
§5.8 asks for five things each turn: verdict summary, open sites with images, op catalogue with schemas and bounds, diffs and deltas of previous candidates, and remaining budgets. All five are present. The budget includes spent and limits for wall, ops, calls and $.

**Implemented (obvious defects):**
1. **The catalogue named 19 ops but explained 6.** Ops like `make_volume`, `conformalise` and `sew` reached the model as a bare name plus a schema.
   - Added `OP_SUMMARIES`: one line per op, from each op's code and docstrings, including which site kinds it serves and that `fill_loop` is followed by `make_volume`.
   - New test: every `REGISTRY` op must have a summary and appear in the prompt. It is red on base and green on head.
2. **The prompt contradicted the checker.** It told the model "no small-edge or small-face errors" is part of Valid, but `verdict/valid.py` makes `small_edge`/`small_face`/`large_tolerance` SOFT codes (design §7.3). A model believing that spends turns on `remove_small`. The prompt now says these are reported but do not fail, and the same test pins it. This fix came after run 2, so no live run exercised it.

**Proposed, not done:**
- a. **The cache-write pricing gap** (section 1). This is the one to do before a paid wave.
- b. **Transcripts are never persisted.** `AIProposer.messages` lives in memory only, and `collect.py` says transcripts "stay in the bucket" but nothing writes them. Write one per file next to the trace, images elided (as `live_readiness.py` does).
- c. **The output reservation dominates the ceiling.** 4096 × $50/1M = $0.205 is reserved per call, but real outputs were 68–106 tokens. A $0.50 file can therefore make calls only until about $0.30 is spent, and a $1.00 file until about $0.80. Either is fine as a bound; just know it. A smaller `REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS` or an explicit reasoning effort would loosen it. Reasoning effort is an owner/lead call: today the API default (`medium`) applies silently.
- d. **The tool schema is loose by design.** `op`/`run_stage` are not required and `params` is a free object; the checks happen in code (`validate_call`), which matches "malformed calls rejected with a structured error". A per-op `anyOf` would let the provider enforce shapes, at the cost of a larger cached prefix. Not urgent.
- e. **`run_stage` params have no schema** ("params: {...}"). Either document each stage's params or drop the field.

## Verification (commands, real exit codes, no pipes)
- `pytest tests/unit/cad/repair_v4` (host venv, PYTHONPATH=src): **612 passed, rc 0** before the rebase; **630 passed, rc 0** after rebasing onto `5c32a632` (Task 47).
- New test against the base `proposer.py`: **1 failed, rc 1** (red). Against head: passes.
- `ruff check` on the 3 changed files: all checks passed. `mypy proposer.py live_readiness.py`: Success.
- **Not run:** the native suite. Under emulation it takes 15+ min, and the change is prompt text only; the native path was exercised by the two live runs instead. Gate VM not used.

## Files
`src/meshpipeline/cad/repair_v4/proposer.py`, `tests/unit/cad/repair_v4/test_proposer.py`, `devtools/validation/v4_eval/live_readiness.py` (new; refuses without `--spend`), this report, and `lane-b-evidence/`.
