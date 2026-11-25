# Lane D9: the AI no longer gives up after one turn

Branch `sdd/lane-d`, rebased on f14e5c4d. Live spend: **$0.5387** (ledger settles, all `search_ai`), under the <= $3 cap.

## The cause

The search driver gave a model generator ONE turn per state expansion, and expanded each state once. When the AI's proposals were declined, failed, or judged worse, no child entered the frontier. The heap emptied, and the search reported `exhausted` with the model's turns and dollars unspent. That happened on 19 of 27 v1 files.

The proposer already carried the right feedback into a next turn:
- `previous_candidates[]`, each with A6's structured `reason` (status/code/detail/stage/op);
- an `observation` with every panel member's status, error kind and located count, plus each site's `meshers_failing`, `failures`, `points` and C5's `coarse` block.

Nothing ever asked it a second time.

## The fix (`search.search`, ~30 lines)

When the frontier is empty and a model generator is still live, the model is asked again on the **best** state. Live means:
- it has not stopped itself (max turns, its per-file $ ceiling, the wave cap, model unavailable);
- `ai_capped` is false;
- the search's model budget has room.

A re-prompt:
- asks **only** the model generators: the deterministic sweep is not re-run;
- reuses the state's sites, pictures and patience: it is not a second expansion;
- is recorded as a `reprompt` trace event (rows: `triage.search.stop.reprompts`);
- counts toward patience, so patience, the per-file $ ceiling, the model budget and the wall still end the search.

No cap changed. With no model generator, the empty frontier is `exhausted` exactly as before.

## Tests

- `test_search.py`, with fake generators:
  - every AI proposal declines -> re-prompted until `patience`, and each re-prompt delivers the decline;
  - no re-prompt once the AI has stopped itself (`file_ceiling`) or the model-call budget is spent;
  - a re-prompt asks only the model, not the deterministic sweep;
  - **search_det is unchanged**: one expansion, then `exhausted`, no `reprompt` event. This test passes on the OLD code too.
- `test_search_wiring.py`: the REAL `AIProposer` with a scripted model. An op decline empties the frontier, the model is asked again, and its turn carries `reason.code == "no_change"` and the observation's sites with their failing members.
- Mutation: disabling the re-prompt fails 4 of these tests; the search_det test still passes.
- Gate: host repair_v4 + dev, 3 env files deselected: 2136 passed, 0 failed. ruff and mypy are clean.

## Live measurement (6 v1 one-turn dev files, search_ai)

Setup: c2d-standard-8, 6 shards x 1 file, --cpus=1, the same settings as v1 (ops 24, h-mm auto, gpt-6-astra, file-usd-cap 0.5, $0.50 per shard). The run used commit 9bd64b64, which PREDATES A6, B8, B8b, B9 and C5.

| file | v1 turns / AI props / AI judged / stop | now turns / AI props / AI judged / full checks / re-prompts / stop | $ v1 -> now |
|---|---|---|---|
| 0061_00615999 (panel) | 1 / 2 / 0 / exhausted | 2 / 5 / 1 / 1 / 1 / file_ceiling | 0.017 -> 0.129 |
| trj4_s1-tu-214 (valid) | 1 / 1 / 0 / exhausted | 4 / 3 / 0 / 1 / 1 / patience | 0.026 -> 0.103 |
| 0051_00519158 (panel) | 1 / 2 / 0 / exhausted | 2 / 4 / 2 / 2 / 0 / file_ceiling | 0.016 -> 0.293 |
| 0060_00606843 (valid) | 1 / 1 / 0 / exhausted | 0 / 0 / 0 / 0 / 0 / wave_cap (no AI) | 0.024 -> 0 |
| 0052_00526988, 00619793 | 1 / 2 / 0 / exhausted | shard refused: route probe "saw pink, not red" (the flake B9 fixed after 9bd64b64) | - |

On the 3 files the AI ran on:
- turns/file 1 -> **2.7**; AI proposals/file 1.7 -> **4.0**; AI-judged states/file 0 -> **1.0**; full-panel checks/file 0 -> 1.3.
- Full passes: 0 (the cheap/full wall was B8's job; that run predates B8).
- No file ended `exhausted` with AI budget left. They ended on patience (1) or on the AI's own per-file ceiling (2).

Findings that bear on the next snapshot:
1. **The per-file $0.50 ceiling binds at about 2 turns**, not 3+. The proposer reserves the worst-case cost of the NEXT turn before asking (a reservation of up to ~$0.29 on these prompts) and stops as `file_ceiling` when that would not fit. So re-prompting is bounded mostly by the ceiling. This is not a cap change; just know that with these settings the AI gets ~2 turns per file.
2. **0060_00606843 got no AI at all** (`wave_cap`, 0 turns): its first reservation did not fit the shard's slice. The same risk exists in a wave.
3. 2 of 6 shards were lost to the route-probe colour flake, which B9 has since fixed.

Raw rows, provenance and ledgers: `lane-d9/` (engine work dirs dropped).
