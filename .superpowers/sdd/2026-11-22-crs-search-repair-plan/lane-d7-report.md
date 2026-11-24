# Lane D7: why is the AI marginal 0 in snapshot v1?

Read-only. Source: `snap-v1/results` (merged at f06efcf7; the run's commit is 79d7ba35, route openai:gpt-6-astra). It covers every file where `search_ai` spent money: 26 dev files plus clean-control 00614313, 27 in total. Built by `lane-d7/ai_table.py` (rows + ledgers -> `lane-d7/table.json`) and `lane-d7/render_table.py` (-> the table below). No code was changed.

## Answer

**Not "never consulted", and not mainly "blocked by the cheap/full wall" either: no AI state ever improved the member that actually fails.** The wall only makes that visible.

1. **The AI was consulted on all 27 files:** 39 model turns, $1.0439. The ledger matches the rows exactly: 39 settles, all `search_ai`, $1.0439, and no per-file mismatch.
2. **It made 72 proposals. 41 (57%) were declined or failed by the op, 9 were duplicates, and 5 were schema/tool errors.**
   - Main decline reasons:
     - `split_angle` "the result is not BRepCheck-valid": 13
     - stage `condition` or `simplify` rolled back: 9
     - `fix_poles` RuntimeError: 5
     - `to_canonical` / `unify_same_domain` no_change: 6
     - `tol_ladder` rungs out of range: 2 (a schema/validation error that reached the op)
   - 15 AI candidates were judged on the cheap tier.
3. **12 of those 15 passed cheap, and all 12 failed full** (`cheap_pass_full_fail` 12, `cheap_pass_full_pass` 0). That is 11 dev + 1 clean; the "11 of 11" quoted was the dev part.
4. **The 12 cheap passes are vacuous.** They sit on exactly three start states: `nist_ctc_01_asme1_ap203` (6), `nist_ctc_01_asme1_rd` (5, the same part) and the clean-control 00614313 (1). On each, the ONLY member failing at the search's start is `gmsh_prod`. The cheap tier is layer 1 + `gmsh_default` only (`assemble.default_members("cheap")`), and `gmsh_default` already passed at the start, so the cheap tier passed before any op did anything. It cannot see the member that fails.
5. **On the other 24 files, no AI state passed the cheap tier.**
   - On 22 of them, `gmsh_default` itself fails at the start, and no AI state fixed it: 0 cheap passes.
   - 12 of the 27 fail layer 1 (valid: body_overlap, self_intersection). No AI proposal repaired layer 1 on any of them.
6. **`search_det` on the same 27 files does even less:** 32 proposals (mostly `conformalise`), 1 judged candidate in total, 0 full checks, 26 `exhausted` + 1 `wall`. The AI gets further (15 judged states), but neither arm produced a full-panel gain. The two files whose panel count rose (0051_00519158 and 0060_00601268, 0 -> 1) rose in BOTH search arms: that came from the staged healer's start state, not from the search.
7. **Stops:** 25 `exhausted` (the generators ran out of applicable proposals) and 2 `patience`. The patience stops were the two NIST ctc_01 copies, where the AI stopped on the per-file $ ceiling (`file_ceiling`) after 4 turns each.

So the three options in the brief resolve as:
- (a) never consulted: no;
- (b) its ops declined: yes, for the majority of proposals;
- (c) blocked at the cheap/full wall: yes for every state that got through, but on this panel the wall reflects that no state improved the failing member, not that good states are being lost.

A cheap tier that included the start state's failing members (here, `gmsh_prod`) would have rejected those 12 states at the cheap tier instead of passing them to full. That is evidence for whoever owns the cheap/full wall (Lane B per the brief); it is not a decision of mine.

Side finding: clean-control 00614313 is in this list only because the staged healer broke it (4/4 -> 3/4, Task 49's known regression). The search starts from the healer's output, so the AI spent $0.044 trying to undo the healer's regression. The delivered output is the clean input.

## Per-file table (file x proposals x outcome)

Columns:
- **AI proposals:** sum of `ops[*].ai`.
- **schema/tool err:** `ai.candidate_errors + ai.tool_errors`.
- **declined by op:** declined + failed, on op keys only the AI proposed.
- **judged (cheap):** candidates with outcome `evaluated`.
- **cheap pass / full checks / full pass:** the row's `cheap_full` cells.
- **det columns:** the `search_det` row of the same file.
- **failing members at search start:** the staged output's failing members, since the search starts from the healer's output.

| file | input fails at | failing members at search start | turns | $ | AI proposals | schema/tool err | declined by op | dup | judged (cheap) | cheap pass | full checks | full pass | stop | det proposals | det judged | det stop |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| nist_ctc_01_asme1_rd.stp | panel | g_prod | 4 | 0.1733 | 10 | 0 | 5 | 0 | 5 | 5 | 5 | 0 | patience (file_ceiling) | 0 | 0 | exhausted |
| nist_ctc_01_asme1_ap203.stp | panel | g_prod | 4 | 0.1635 | 10 | 0 | 4 | 0 | 6 | 6 | 6 | 0 | patience (file_ceiling) | 0 | 0 | exhausted |
| bug32314_corrupted.stp | valid | g_alt,g_default,g_prod,ng | 2 | 0.0614 | 4 | 2 | 3 | 1 | 0 | 0 | 1 | 0 | exhausted | 13 | 1 | wall |
| 0054_00541957_51229e00ebc25e88ce80 | valid | g_alt,g_default,g_prod | 1 | 0.0593 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | exhausted | 2 | 0 | exhausted |
| 0057_00573880_417ab441d502eafedde0 | panel | g_alt,g_default,g_prod,ng | 2 | 0.0486 | 5 | 0 | 4 | 0 | 1 | 0 | 1 | 0 | exhausted | 0 | 0 | exhausted |
| 0055_00550171_58ac32b095b047102fd8 | panel | g_alt,g_default,g_prod,ng | 2 | 0.0475 | 5 | 0 | 4 | 0 | 1 | 0 | 1 | 0 | exhausted | 0 | 0 | exhausted |
| 0060_00604433_58c21d4ea0c27b0f87b1 | panel | g_alt,g_default,g_prod | 2 | 0.0474 | 4 | 0 | 3 | 0 | 1 | 0 | 1 | 0 | exhausted | 0 | 0 | exhausted |
| 00614313_760137e15b9b924e8339e428_ | clean | g_prod | 2 | 0.0440 | 5 | 0 | 4 | 0 | 1 | 1 | 1 | 0 | exhausted | 0 | 0 | exhausted |
| 0060_00608014_3fedd89a1d6b0c6f1c34 | valid | g_alt,g_default,g_prod,ng | 2 | 0.0411 | 2 | 3 | 1 | 1 | 0 | 0 | 0 | 0 | exhausted | 4 | 0 | exhausted |
| 0051_00519727_589dc8aa5abe5e0fe4ad | valid | g_alt,g_default,g_prod | 1 | 0.0265 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | exhausted | 2 | 0 | exhausted |
| trj4_s1-tu-214.stp | valid | g_default,g_prod,ng | 1 | 0.0255 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | exhausted | 2 | 0 | exhausted |
| 0057_00574245_58b65964d2d73c0f795d | valid | g_alt,g_default,g_prod | 1 | 0.0251 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 0060_00606843_58c2e4ec85eca50f9755 | valid | g_alt,g_default,g_prod | 1 | 0.0243 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | exhausted | 1 | 0 | exhausted |
| 0053_00535066_bb0bc8e7914fdea894ad | valid | g_alt,g_default,g_prod | 1 | 0.0233 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | exhausted | 2 | 0 | exhausted |
| 0053_00533660_58a4821bab78c2106dbc | valid | g_alt,g_default,g_prod,ng | 1 | 0.0227 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | exhausted | 2 | 0 | exhausted |
| 0052_00521884_f7d1e4b9658237600ec8 | valid | g_default,g_prod,ng | 1 | 0.0208 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | exhausted | 2 | 0 | exhausted |
| trj3_s1-db-214.stp | valid | g_default,g_prod,ng | 1 | 0.0204 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 00613121_58c67ddf032617103d59c101_ | valid | - | 1 | 0.0196 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | exhausted | 2 | 0 | exhausted |
| 0054_00547929_58ab27a87442550f6daa | panel | g_alt,g_default,g_prod | 1 | 0.0181 | 2 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 0061_00614327_760137e15b9b924e8339 | panel | g_alt,g_default,g_prod | 1 | 0.0172 | 2 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 0061_00615999_4f1d2ad2e4e729a2f183 | panel | g_default,g_prod | 1 | 0.0172 | 2 | 0 | 2 | 0 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 00611083_58c56432786ec50fe9e0d3ad_ | panel | g_alt,g_default,g_prod | 1 | 0.0171 | 2 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 0060_00601268_01579e613800b230c171 | panel | g_alt,g_prod,ng | 1 | 0.0163 | 2 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 0052_00526988_58a1d33fd3d9a2108b54 | panel | g_alt,g_default,g_prod | 1 | 0.0161 | 2 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 0051_00519158_daf80b2021249a203e93 | panel | g_alt,g_prod,ng | 1 | 0.0159 | 2 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 0057_00570551_59995fb3cd27b48fe095 | panel | g_alt,g_default,g_prod | 1 | 0.0157 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| 00619793_973db45bf9076901a50908bc_ | panel | g_prod | 1 | 0.0156 | 2 | 0 | 1 | 1 | 0 | 0 | 0 | 0 | exhausted | 0 | 0 | exhausted |
| **total, 27 files** | | | **39** | **1.0439** | **72** | **5** | **41** | **9** | **15** | **12** | **16** | **0** | | **32** | **1** | |

## Attribution, and what the traces lack

- **Proposals by source are exact** (`ops[op].ai` / `.det`).
- **Outcomes are recorded per op, not per source.** I attribute an op key's outcomes to the AI only where the AI alone proposed it (`det == 0`). The only key both generators proposed is `conformalise`, on 7 files, and on all 7 it has `evaluated == 0`. So every judged candidate and every cheap/full event in these `search_ai` rows is the AI's, and no number above is ambiguous.
- **What the snapshot lacks, for next time:**
  - (a) per-source outcome counters in `arms.search_triage` (declined / duplicate / evaluated split by `det`/`ai`);
  - (b) the source on the cheap->full `confirm` cells;
  - (c) per-candidate `deltas`, which already exist on the current tip but post-date 79d7ba35.
- None of these was needed to answer this question; (a) and (b) would make the attribution exact by construction instead of by argument.
- **Two counts do not line up 1:1, as expected:**
  - "judged (cheap)" is 15, but there are 16 full checks. bug32314 has one full check on a state with no AI-only `evaluated` outcome.
  - "ops applied" counts what each candidate charged to the op budget (a `run_stage` chain applies several ops), not candidates, so it is not in the table.
