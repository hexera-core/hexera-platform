# Lane B2 report: proposer prompt and schema, generated from the checker

Branch `sdd/lane-b` rebased on `f0f901a3` (the lead's merge of lane B). The ledger is Task 50's and is not touched here. One live call budget: one `search_ai` run, cap $0.50.

## Headline
**The prompt's definition of "clean" is now written from the checker's own constants, so the two can no longer disagree.** A unit test proves it: changing a constant changes the prompt.
**`run_stage` now has a real interface.** The stage catalogue lists each stage's parameters and defaults, and a misspelt key or stray targets come back as a structured error instead of being dropped silently.
**Live re-run: clean in 2 turns, $0.0694.** That is the same turn count as both earlier runs, and the model now reasons with the generated numbers: "Its 4.24 mm span is below the 6.15 mm fill limit".

## What changed (`src/meshpipeline/cad/repair_v4/proposer.py`)
1. **`_clean_definition()` builds the WHAT CLEAN MEANS section of the system prompt from the checker:**
   - Layer 1: `verdict.valid.HARD_CODES`, each with a one-line description from `HARD_CODE_TEXT`; a test requires one per hard code. `SOFT_CODES` are listed by name and stated **not** to fail.
   - Layer 2: `verdict.mesh_child.MIN_SICN` and `verdict.assemble.MEMBER_TIMEOUT_S`.
   - Layer 3: `verdict.faithful_kernel.BOUND_ABS_MM`/`BOUND_DIAG_FRACTION`/`RATIO_TOLERANCE`, and `limits.MAX_FILL_SPAN_RATIO`.

   This fixes the lane B contradiction at its root: the small-edge/small-face wording came from hand-typed prose, which now no longer exists. Writing it from the code also surfaced three smaller errors in the old prose:
   - **"No face is lost except a declared sliver below the threshold" was wrong.** The checker exempts every declared removal, of any size, and measures declared adds one way only. The prompt now says exactly that.
   - **"No body is lost" was wrong in both directions.** The checker fails any change in body count, and skips both the body-count and the volume rule when the original holds no solid (the hole-box case: building the solid is the repair). The prompt now says that too.
   - **"Area and volume stay under their limits" now gives the limit**: 2%.

   The prompt also says that findings name solids and shells by position (S1, H1), while ops take stable ids (B2). The model would otherwise see `S1` in `valid.hard` and pass it as a target.
2. **`run_stage`:**
   - The catalogue line is generated from `stages.DEFAULTS`, one line per stage with its keys and defaults. It used to say only `"params": {...}`.
   - `_check_candidate` refuses param keys the stage does not read, naming the real ones. `Stage.execute` merges params over the defaults, so a misspelt key was silently ignored and the model never learned why its stage did nothing.
   - It also refuses `targets` on `run_stage`, which the search drops. The existing test that passed `{"tol": 1}` to `faces` (the real key is `tol_mm`) pinned exactly that silent acceptance; it now passes `tol_mm`.
3. `fill_loop`'s summary takes its cap from `MAX_FILL_SPAN_RATIO` instead of a typed "5%".

## Proof with fakes
- New tests in `tests/unit/cad/repair_v4/test_proposer.py`:
  - every hard code is described and listed;
  - soft codes are never listed as hard;
  - the numeric limits appear, and monkeypatching `MIN_SICN`, `BOUND_ABS_MM` and `SOFT_CODES` changes the prompt;
  - the stage catalogue matches `DEFAULTS`;
  - a bad stage key and stray targets each get a structured error while the valid candidate in the same call is accepted.
- **Red on base** (base `proposer.py` with the new tests): `3 failed, 67 passed`, rc 1. **Green on head:** `70 passed`, rc 0.
- `PYTHONPATH=src pytest tests/unit/cad/repair_v4` (host venv): **633 passed, rc 0**. `ruff check` on both changed files: clean. `mypy proposer.py`: Success.
- Not run: the native suite. The change is prompt text and pre-dispatch validation; the native path was exercised by the live run below.

## The one live re-run (search_ai, 3x3 mm hole box, `openai:gpt-6-astra`, cap $0.50)
`live_readiness.py search --spend --usd 0.50 --turns 6 --ops 12 --wall-s 600`, in `meshpipeline-mesh:native`, through the production `repair_v4()`.

| run | prompt | result | turns | model $ (ledger) | cache on call 1 | ops | wall |
|---|---|---|---|---|---|---|---|
| 1 (lane B) | base | clean | 2 | $0.0368 | warm (2338 cached, from the probe) | 10 | 88 s |
| 2 (lane B) | + op summaries | clean | 2 | $0.0657 | cold (4057 written) | 10 | 109 s |
| **3 (this)** | + generated clean definition + stage catalogue | **clean, delivered, 4/4 panel, faithful** | **2** | **$0.0694** | cold (4435 written) | 7 | 74 s |

- **Turns: unchanged.** The model again proposed `fill_loop` on E21–E24, then `make_volume`. The winning path S0→S1→S3 again came from the det candidates, which were evaluated first; the AI's identical candidates were duplicates.
- **Dollars:** +$0.004 against run 2, because the static prefix grew by about 380 tokens (4057 → 4435) and this first call was a cache miss. A warm second file would pay about $0.004 per call for the prefix instead of about $0.044. Run 1 was cheaper only because the probe had warmed the cache.
- Ops fell from 10 to 7 because of Task 47 (merged before this run), not this change.
- **What the model wrote, run 3, turn 1:** "Its 4.24 mm span is below the 6.15 mm fill limit; close it with a declared face before building the solid." 6.15 mm is 5% of the 123 mm diagonal; the model computed it from the generated text. Runs 1–2 gave no such check.
- Cache-write tokens on this run: 5647. The ledger under-records them by about $0.014, which is Task 50's fix.
- Evidence: `lane-b-evidence/search-run3/` (transcript, trace, raw wire usage, ledger).

## Judgement calls, listed and not implemented
- **Transcript persistence.** Neither `repair_v4()` nor `run_arm` writes the proposer conversation; only `live_readiness.py` does. Where it lives (beside the trace in the eval bucket? on the production path at all, given customer geometry ids and images?) is a lead/owner call.
- **Output reservation and reasoning effort.** Each call reserves 4096 output tokens ($0.205 on astra), while real outputs are about 70–110 tokens, so a $0.50 file stops being admitted at about $0.30 spent. Effort is unset, so the API's default `medium` applies; it produced 0 reasoning tokens on every call so far. Lowering the reservation or pinning an effort changes what the model can do, and the ledger lane may want to own it.
- **Per-op `anyOf` in the tool schema.** It would let the provider enforce each op's params, at the cost of a larger cached prefix. Today the code validates after the call and returns a structured error, which design §5.8 accepts.
- **This change grew the first-call input by about 9% (4060 → 4438 tokens).** That is cheap while the cache is warm, but the prefix is worth watching as op summaries and stage lines grow.
- **AI marginal value is still unshown on this case.** An AI-only ablation (det generator off, about $0.07) is the cheapest way to show it. It was not run because the brief allowed one live run.
