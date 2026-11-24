# Lane B16: where search_ai and search_det diverge on v3b — and why the AI's marginal is +0

Wave `snap-v3b-202611240606` on `e2b25ddf` (the repaired frontier plus C11), PARTIAL: 26 dev shards and 2 clean
shards had written rows at 07:10Z, and the wave root carries **107 traces**. Tool: `lane-b16/divergence.py`
(3 tests). Full table: `lane-b16/v3b_divergence.md`.

**Coverage.** 45 paired files, both arms conclusive and both traces present: **40 clean-split + 5 dev**. 12 more are
unpaired — 6 one-arm-only, 6 inconclusive — and section 4 is about those, because that is where the answer is.

## 1. The paired table says nothing happened

| | |
|---|---|
| first divergence | **never, on all 45**: both arms took the identical path every time |
| better end | **equal, 45 of 45** |
| clean pairs | 43 both clean, 2 both not clean |
| AI ops on a delivered path | **0 of 45** |
| stop reasons | det: clean 43, patience 2. ai: clean 43, patience 2 |

Read alone, that is "the AI changes nothing". It is also nearly vacuous: 40 of the 45 are clean-split controls that
are clean at input and finish at depth 0, and of the 5 dev files, 3 go clean at depth 0–1 without either arm
needing a choice. **Only 2 paired dev files involve a search at all.** The paired view is not where the comparison
lives.

## 2. The AI does get turns, and its proposals are competitive

Across the dev traces, AI-sourced candidates: **8 ACCESS** (no det candidate at that state named that op),
**3 JUDGEMENT** (det named it there too), **4 DUPLICATE**, **4 declined**. Ops only the AI proposed, at the state
where it proposed them: `conformalise{fragment}`, `fuse`, `remove_small`, `run_stage`, `split_angle`,
`split_by_proximity`, `split_closed`, `unify_same_domain`, `reapproximate`, `rebuild_edge_as_intersection`.

Whether a proposal was GOOD can only be asked between siblings — children of one state are all scored on the same
cheap tier, where a cheap child against a full parent is not comparable (lane D's correction to my B13). Among the
17 states with an AI-sourced evaluated child:

| sibling verdict | states |
|---|---|
| **AI's best child beat det's best child** | **5** |
| det's best child beat the AI's | 5 |
| tie | 2 |
| **the AI was the ONLY proposer at that state** | **5** |

So the AI's access is real (5 states det had nothing for) and its judgement is about a coin flip against det's own
sweep. That is the honest answer to "access or judgement": **it has some of both, and neither is the binding
constraint.**

## 3. AI ops ARE on a best path — in three traces

`5abbaf0b5508` (2 ops), `615e4cde0518` (2), `bcf201b55323` (1). All three are dev files. None of them appears in
the paired table, because **all three rows are `inconclusive`.**

## 4. The mechanism: the AI is penalised for exploring, and its row is then discarded

| file | search_det (KEPT) | search_ai (DISCARDED) | the AI's best state, from its trace |
|---|---|---|---|
| `bcf201b55323` | **exhausted in 60 s**, 6 ops, panel 2→2 | **wall at 668 s**, 38 ops, $0.45, inconclusive | **panel 3**, 5-state path, an AI `split_angle` on it |
| `615e4cde0518` | exhausted in 50 s, 4 ops, panel 2→2 | wall at 744 s, 29 ops, $0.25, inconclusive | panel 2, min SICN 0.0085, 4-state path, an AI `fuse` on it |
| `5abbaf0b5508` | exhausted in 61 s, 5 ops, panel 0→0 | wall at 649 s, 41 ops, $0.61, inconclusive | panel 0: no gain |
| `cbe8c3abc11c` | wall, 4 ops, inconclusive | wall, 6 ops, inconclusive | panel 1 |
| `5e9f31e673fd` | wall, 24 ops, inconclusive | wall, 36 ops, inconclusive | not valid |

The rule doing this is explicit, in `measurement.file_unmeasured`:

```python
or (reason in TIME_STOPS and not out_clean)
```

> "A search that stopped on its wall without ending clean was not measured either: that is the machine's time, not
> a verdict."

Put together with the arms' behaviour, that rule is an asymmetry:

- **det exhausts its candidate list in 50–60 s** on these files and gets a conclusive row.
- **the AI keeps proposing for 650–750 s**, applies 6–8× the ops, reaches states det never reached — and hits the
  wall. Its row is then recorded as inconclusive, with `out_panel_passes: None`, and counts for nothing.

On `bcf201b55323` the discarded AI row's best state **passes 3 panel members where det's kept row passes 2**. That
is a repair the benchmark threw away. The evidence survived only in the trace, which is exactly why asking for
traces was the right call.

**So: the AI is not failing to get a turn, and it is not failing on access. It gets the turn, uses it, reaches a
better state on at least one file, and the measurement rule discards the row for having taken too long.** An arm
that explores more is structurally more likely to be deleted from the measurement than an arm that gives up early.

## 5. What I would change, in this order

1. **Report a wall-stopped search as MEASURED, with `stopped_on_wall: true`** — owner call, because it changes a
   measurement rule. The justification for discarding it does not hold up: the best state has a CONFIRMED full
   verdict (its `confirm` event is in the trace), and `deliverable` already refuses to ship anything worse than the
   input on (valid, panel). Keep such rows out of the clean-rate denominator if that is wanted, but "panel 2→3,
   stopped on the wall" is a measurement, not machine time. On this partial wave alone this converts 1 discarded
   row into a visible panel gain and 2 more into visible ties, which is the AI marginal moving off +0.
2. **Stop charging the AI's wall for model latency.** det's work is all local; the AI's 650 s includes 3–7 model
   round trips plus any throttle wait. Equal wall is not equal opportunity. Either subtract the measured model wait
   (`model_throttle` already records holds and `waits_timed_out`) from the search wall, or budget the arms by
   expansions/ops instead of seconds. `ops` never binds (D15: max 45 of 60), so an ops-based budget would bind on
   nothing today.
3. **Only then the op library.** On the one paired dev file where the AI proposed heavily (7 ACCESS candidates
   including `fuse` and `conformalise{fragment}`), every AI child lost to det's best sibling, and the file still
   fails `gmsh_prod/other` and `gmsh_default/edge_through_face`. Adding ops would not have helped there; the
   proposals were available and worse.

## 6. What the traces gave, and what they still cannot

**Gave:** every candidate's op, params, source, the proposer's own rationale, outcome and first decline reason; the
child's cheap score and its parent's; `confirm` events with the FULL score of confirmed states (which is how §4's
"best state" column exists for rows that carry no verdict at all); each expansion's sites; reprompts; `best_path`.

**Cannot:** a full-panel verdict for an unconfirmed candidate — so every comparison here is cheap-against-cheap
between siblings, or confirmed-against-confirmed, and never across tiers; the op's own measured numbers (a cut's
depth, a fill's deviation), which live in the Diff; the staged healer's internal transactions (the row's
`triage.stages` has those); and any counterfactual — whether an op nobody proposed would have worked. v3b's rows
carry `located`, so a mesher's own named faces are available where it named them, but no trace says WHY the AI
chose what it chose beyond its rationale text.

**Scope:** 10 dev `search_ai` rows and 17 dev `search_det` rows exist so far, of 150 files. Half the dev AI rows are
inconclusive. Section 4's mechanism rests on 5 files; sections 1–2 on 45. I would not quote a rate off this.
