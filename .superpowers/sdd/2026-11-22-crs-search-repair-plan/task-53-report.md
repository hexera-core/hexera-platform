# Task 53 report: the op budget is charged per APPLIED op, for every arm

Branch `sdd/task-53` (worktree `/Users/kitts/archductor/sdd-worktrees/task-53`), based on `de96441c`.
Nothing merged, nothing pushed.

## The ruling, as implemented

`search`'s op budget now charges what the op library **actually applied** for a candidate:

- a plain registered op applies exactly one op and costs **1** (unchanged);
- a `run_stage` macro costs **every op its stage body applied** — `StagedResult.applied_ops[name]`,
  counted from the `Diff`s the ops returned, never from a table of stage names;
- a candidate that was run but applied nothing (declined, failed, or the stage changed nothing) still
  costs **the attempt, 1**. That is the pre-existing semantics (the budget was charged before the ops
  ran) and it is the same for both arms, so it was not changed; `max(1, applied)` is the charge.
- a rolled-back stage, and a chain abandoned halfway (`_ChainBroken`, e.g. sew applied and then
  `make_volume` declined), are charged what they applied: that kernel work was really done.

The rule is in the driver, not per generator, so it applies to `search_det` and `search_ai` alike.

## Which stages are multi-op, measured

Measured, not read off a table: every op in `ops.REGISTRY` was replaced by a fake that returns one
`Diff`, and each stage was run at its **default** params on a 2-face part with and without a solid
(throwaway script, not committed):

| stage | applied, with a solid | applied, no solid | ops |
|---|---|---|---|
| baseline | 0 | 0 | — |
| normalise | 1 | 1 | tighten_tolerances |
| edges | 1 | 1 | fix_poles (+ rebuild_edge_as_intersection when `targets` is given → **2**) |
| faces | 1 | 1 | to_canonical (+ one rebuild_face per id in `rebuild` → **1 + len(rebuild)**) |
| shells | 1 | **2** | orient_and_close / **sew + make_volume** (+ remove_small, + fill_loop when `small` / `fill_edges` are given → up to **4**) |
| solids | 1 | 1 | orient_and_close / make_volume |
| bodies | 0 (needs ≥2 solids) | 0 | conformalise → 1 |
| simplify | 1 | 1 | unify_same_domain |
| condition | **3** | **3** | **split_angle + split_closed + split_continuity** (+ split_by_proximity with `face_pairs` → up to **4**) |
| finalise | 1 | 0 | orient_and_close |

**`condition` is the biggest one and Lane C3 did not name it: 3 ops at its defaults, 4 with
`face_pairs`.** Lane C3 named `shells` (2 without a solid, up to 4). So under the old accounting a
single `run_stage: condition` candidate bought three applied ops for one budget unit.

## What changed

- `src/meshpipeline/cad/repair_v4/stages.py`
  - `StagedResult.applied_ops: dict[str, int]` — per stage, how many ops of its body applied, counted
    from `_Steps.diffs`. Filled on every path: kept, rolled back, skipped, inconclusive, chain broken.
  - `_ChainBroken` now carries `applied`, so the ops that ran before a required op declined are still
    reported (the skipped-message text is unchanged).
- `src/meshpipeline/cad/repair_v4/search.py`
  - `_Outcome.applied`; `_stage_outcome` reports it on every return path; a plain op reports 1.
  - the batch still reserves one op per candidate it runs, then adds `sum(max(0, applied - 1))` once
    the outcomes are in.
  - `SearchState.ops_applied` and `SearchResult.path_steps[i]["ops"]` report the per-step cost
    (`recipes.recipe_from_path` reads `via` / `sites` only, so it is unaffected).
  - trace `candidate` events carry `ops_applied` (evaluated, duplicate, inconclusive, rejected).
  - module docstring: the budget section and the STAGES wiring bullet now state the real rule.
- `src/meshpipeline/cad/repair_v4/proposer.py` — the op catalogue handed to the MODEL said
  `run_stage` was "one candidate, one op of budget". That was the asymmetry stated as an invitation;
  it now says the budget is charged every op the stage applies, with `shells` as the example.
- `devtools/validation/v4_eval/`
  - `arms.search_triage` sums the trace's `ops_applied` per op, and `triage.py` aggregates and prints
    it as an `ops charged` column: the per-op table's other columns are candidate counts, this one is
    budget units.
  - `evalspec.Budget` docstring and `run_arm.py --ops` help now say the unit is applied ops.
  - `stop.spent.ops`, which the eval reads and prints, comes straight from the driver's counter, so it
    was already in whatever units the driver counts — it is now in applied ops with no change needed.

### Readers checked

`over_budget()` / `spent_now()` (termination and the per-expansion check), the batch truncation
(`room`), `SearchResult.path_steps`, the `stop`/`ai_capped`/`candidate` trace events,
`arms.py` (per-file `Budget` → `search.Budget`, `search_triage`), `triage.py`, `report.py` (echoes the
configured budget only), `run_arm.py --ops` (lower-only override), `live_readiness.py --ops` (passes
through), `evalspec.Budget`. Production (`cad/repair/conservative.py`) builds the v4 budget with
`model_calls=0`, so there is no AI proposer and no `run_stage` candidate there; `CAD_REPAIR_MAX_OPS`
behaviour in production is unchanged by this task.

## Known limit, stated

`budget.ops` is now a bound, not a hard cap: a chain's length is only known after its body ran, so one
expansion can finish a few ops over the budget (at most the excess of the chains in that one batch).
The overrun is counted in `spent` and the next expansion stops the search. The alternative — refusing
to run a candidate that might be a chain — would need a cost table keyed by stage name, which is
exactly the thing the brief rules out. The test `test_the_op_budget_counts_the_chain_and_stops_the_search`
pins this behaviour: budget 3, two 2-op chains, spent 4, stop reason `ops`.

## Red, then green

Written first, run red:

```
PYTHONPATH=src python -m pytest tests/unit/cad/repair_v4/test_stages.py \
  tests/unit/cad/repair_v4/test_search_wiring.py -q     # exit 1: 7 failed, 105 passed
```

The failures that matter, before the change:

- `test_a_multi_op_stage_costs_its_real_applied_op_count`: `assert 1 == 2` — the sew + make_volume
  chain cost **one** op of budget.
- `test_the_op_budget_counts_the_chain_and_stops_the_search`: `assert 'exhausted' == 'ops'` — two
  chains (four real ops) did not exhaust a 3-op budget.
- `test_a_single_op_candidate_still_costs_one`: `KeyError: 'ops_applied'` (the trace did not report it).
- the four `test_stages.py` tests: `AttributeError: 'StagedResult' object has no attribute 'applied_ops'`.

New tests (9 new, 2 existing extended): `test_stages.py` — a chain reports 2, a single-op stage reports 1, a rolled-back stage
and a broken chain report the ops that ran, a stage that applied nothing reports 0.
`test_search_wiring.py` — a multi-op stage candidate costs 2 (budget, trace and `path_steps`), a
single-op candidate still costs 1, and the budget stops the search on real applied ops. One existing
test (`test_a_stage_the_checker_could_not_judge_is_inconclusive_not_failed`) had a `SimpleNamespace`
fake of `StagedResult` that predates the new field; its fake was completed and it now also asserts
that an unjudged stage's ops are still charged. Two eval tests
(`tests/unit/dev/test_v4_arms.py`, `test_v4_triage.py`) were extended to pin the new `ops_applied`
key and the new table column, since their per-op table gained one.

## Gates

Run with the interpreter noted under "risk" below (`V=/Users/kitts/archductor/sdd-worktrees/task-23/.venv/bin/python`,
cpython 3.12.13, pytest 9.0.3 as pinned in `requirements/runtime.txt`), from
`/Users/kitts/archductor/sdd-worktrees/task-53`, each redirected to a file and then grepped - never
piped:

```
# the tiers this change can touch, complete, on the final code
PYTHONPATH=src $V -m pytest tests/unit/cad/repair_v4 tests/unit/dev/test_v4_arms.py \
                           tests/unit/dev/test_v4_triage.py -q    # exit 0: 797 passed in 295.57s

PYTHONPATH=src $V -m ruff check <11 changed files>                 # exit 0: All checks passed!
PYTHONPATH=src $V -m mypy <7 changed src+devtools files>           # exit 0: no issues in 7 source files
PYTHONPATH=src $V -m ruff format --check <11 changed files>        # exit 1 - fails at HEAD too, see below
```

### The full unit tier: NOT completed, and why - read this, do not assume it passed

`PYTHONPATH=src $V -m pytest tests/unit -q` **cannot complete on this host right now**, for two
reasons, neither of them this change:

1. **It does not even collect.** Exit 2, `Interrupted: 5 errors during collection`, all five the same
   `ImportError: cannot import name 'FakePublisher' from 'conftest'
   (.../tests/unit/cad/repair_v4/conftest.py)` in
   `tests/unit/engines/test_user_engine_control.py`, `tests/unit/worker/test_dispute_flow.py`,
   `test_final_result_containment.py`, `test_terminal_authority_mutations.py`,
   `test_worker_terminal_matrix.py`. Those modules do `from conftest import FakePublisher`, meaning
   `tests/unit/conftest.py`, and the name `conftest` is claimed by
   `tests/unit/cad/repair_v4/conftest.py` when both are imported in one session.
   **It is not mine:** it reproduces with two files I never touched -
   `pytest tests/unit/cad/repair_v4/test_ops_registry.py tests/unit/worker/test_dispute_flow.py`
   exits 2 with that exact error, and `git diff --name-only` shows no conftest and nothing under
   `tests/unit/worker/` or `tests/unit/engines/` in my change. Each of the five modules passes on its
   own: run together, `55 passed, exit 0`.
2. **Host contention.** `pytest tests/unit -q --continue-on-collection-errors` gets past the five
   errors and runs, but this machine has 10 cores at load average 220-250 (several other agents
   running their own full tiers): collection alone took 4 minutes and the run then advanced at
   roughly 0.4 tests/s - 3% of the tier in 26 minutes, i.e. of the order of ten hours for all of it.
   I stopped it at 3% (about 790 tests, no failure up to that point) rather than hold a saturated
   host for hours; a `tests/unit/dev`-only run was no faster (72 tests in 8 minutes) and was stopped
   too. **I am not claiming the full tier green.** No process I did not start was touched.

What IS proven on the final code: the complete `tests/unit/cad/repair_v4` tier plus both eval
modules that read the op accounting - which are the only files I changed under `tests/unit/dev` -
(797 tests, exit 0), and the five modules the collection bug hides (55 tests, exit 0). Everything I
touched lives in `repair_v4`, `devtools/validation/v4_eval` or those test files; nothing outside them
imports `StagedResult.applied_ops`, `_Outcome.applied` or the new trace key. Anyone merging this should run `make test-fast` on an idle host, with
`--continue-on-collection-errors` or a fix for the conftest collision.

`ruff format --check` fails, and it **fails the same way on `HEAD`**: `git show
HEAD:src/meshpipeline/cad/repair_v4/search.py | ruff format --check --stdin-filename search.py`
exits 1 before any change of mine. This repository is not ruff-format-formatted (`make lint` runs
`ruff check` only), so I did not reformat the files; doing so would have buried the change in a
whole-file diff. `ruff check` passes on every changed file, which is the gate the Makefile runs.

Not run: `make test-native` / the OCCT tier. It needs the `meshpipeline-mesh:native` image on an x86
host and there is no gate VM up; I did not run it and nothing here is verified on the real kernel. The
change is pure accounting (no new kernel call, no op behaviour), and the stage bodies it counts are
exercised natively by the existing `tests/native/test_native_repair_v4_stages.py`.

## Does this alter the T2 smoke's arm comparison?

**No, not the run that exists — and the asymmetry it removes favoured `search_ai`.**

- `search_det` is unaffected, now and before: `DetGenerator` never proposes `run_stage` (Lane C3
  decided not to add it), so every deterministic candidate applies exactly one op and was charged one.
- `run_stage` is reachable only by the AI proposer (`ai_generators` passes `stage_names` to
  `AIProposer`). So at the same `--ops`, `search_ai` was previously able to buy **more real ops** than
  `search_det` — up to 3 applied ops per budget unit through `condition`, 2 through `shells` — for
  every multi-op stage it proposed. The AI marginal measured that way would have been overstated by
  the extra geometry work, not only by judgement.
- In Task 49's T2 smoke that advantage never materialised, so no published number moves:
  `search_ai` **was not run at all** (halted by the coordinator, 0 settled model calls), and
  `search_det` stopped on `ops` in **0 of 28** rows (26 `exhausted`, 1 clean, 1 wall). The smoke's
  numbers are unchanged by this task.
- For the **next** `search_ai` run the effect is the reverse of an advantage: at `--ops 24` an AI arm
  that leans on stages will now get fewer candidates than it would have, because each chain costs what
  it applies. That is the intended correction, and it means an AI marginal measured before this commit
  is not comparable with one measured after it.

## What I did NOT do

- I did not change what a declined or failed candidate costs (still the attempt, 1). Making declines
  free is a different policy question and would change both arms' budgets.
- I did not give `DetGenerator` `run_stage` candidates (Lane C3's other option for fixing the
  asymmetry); the ruling was to charge per op.
- I did not touch `CAD_REPAIR_MAX_OPS`, its help text, or the v3 repair loop that shares the setting.
- I did not re-tune `evalspec.Budget.ops` (60) or any arm's budget for the new unit. If an arm is
  expected to run several multi-op stages, 60 applied ops is a smaller search than 60 candidates was;
  that is a wave-planning call, not mine.
- No deploy, no model call, no native run, no merge, no push.

## Risk handed back

1. **The full unit tier was not completed** (see the gates section): a pre-existing conftest-name
   collision stops `pytest tests/unit` from collecting at all, and the host is at load ~220 on 10
   cores, so the `--continue-on-collection-errors` run was still going. The repair_v4 and eval tiers
   are green; the rest of the tier is unproven on this commit.
2. **The interpreter.** This worktree has no `.venv` and `make setup` was not run here; the gates
   above used the venv of a sibling worktree (`sdd-worktrees/task-23/.venv`) read-only, with
   `PYTHONPATH=src` so every import resolved into *this* worktree (verified:
   `meshpipeline.__file__` pointed at `task-53/src`). If that worktree is destroyed, the gates must be
   re-run against a fresh environment.
3. **The budget overrun** described above (at most one batch's excess, reported honestly as spent).
4. **`condition` costs 3**: any budget sized on the assumption that a stage is one op (including the
   production floor discussion in Task 47's report, 3 holes = 14 ops) should be re-read with the table
   above in hand.
5. The eval's per-op table gained a column (`ops charged`); any downstream parser of
   `triage.py`'s markdown or of `arms.search_triage`'s JSON sees one new key, `ops_applied`.
