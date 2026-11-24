# Lane B8b report: a state-adaptive cheap tier (after D7)

Follow-up to B8 (`lane-b8-report.md`), asked for by LEAD-2 and the coordinator on the strength of D7's evidence. D7 showed that v1's 12 cheap passes were vacuous: the cheap tier already passed the START state, because gmsh_default passed there and gmsh_prod, the only failing member, was not meshed. On the files where gmsh_default fails, the AI produced no cheap passes at all.

## The options, measured on the same 22 native states (`lane-b8/*.b8.json`)
The adaptive options use the search's START state. In v1 that was the cheap-only staged state where there is one, otherwise the input.

| Cheap tier | Agreement, all 22 | Agreement on the 13 non-start states | Mean cheap meshing per state |
|---|---|---|---|
| A: layer 1 + gmsh_default (before B8) | 6/22 | 2/13 | 0.34 s |
| B: + gmsh_prod, static (B8, merged) | 21/22 | 13/13 | 1.45 s |
| C: gmsh_default + every member the START fails | **22/22** | 13/13 | **1.35 s** |
| **D: B + every member the START fails (chosen)** | **22/22** | 13/13 | 1.47 s |

**Why D and not C.** C measured best here only because 00614313's v1 start was the pre-B5 *damaged* staged state, which already failed gmsh_prod. On a **clean** start, C is gmsh_default alone. The staged healer's `condition` stage then breaks gmsh_prod unseen again, which is exactly B5's regression, and only `confirm_staged` would catch it. D keeps gmsh_prod on every start and adds whatever the start fails, for 0.02 s more than B per state. It matches the coordinator's instruction: gmsh_default plus every member failing at the start, gmsh_prod included.

**What the adaptive part adds beyond B on v1's data: no measured state.** On v1, every input that fails gmsh_alt or Netgen also fails gmsh_default or gmsh_prod (`lane-b8/v1_inputs_agreement.py`). It matters once a search fixes gmsh_default and gmsh_prod on such a file: B would then cheap-pass a state that gmsh_alt or Netgen still fails, and D would not. The cost lands on exactly those files: the start-failing members are meshed in every cheap verdict (Netgen about 2.5 s, gmsh_alt about 0.7 s).

## The change
- **`verdict.assemble.adaptive_cheap_members(start_verdict)`.** It returns the static cheap members plus every full-panel member the start's full verdict does not pass, using the full panel's own member objects and configs. Unknown names are ignored, and `None` gives the static tier.
- **Search.** The start is judged FULL as before. `search.adaptive_cheap(judge, start_verdict)` builds this search's cheap verdict over those members, through the new optional `Judge.cheap_with`; a judge without it, such as a test's, keeps `cheap`. Every child's cheap verdict and the start's cheap score use it.
- **Staged healer.** `run_staged(..., cheap_members=)` threads the members into every transaction (`_transact` → `_judged` → `_judge` → `cheap_verdict(members=)`). The engine's staged arm and `_find_best` pass `adaptive_cheap_members(input_verdict)`. `_find_best` now judges the input fully up front when it was not handed a verdict, then confirms with it. The eval arms pass the same through `RealEngine.run_staged`.
- **Unchanged:** the full panel, `clean_of`, every full verdict, and `default_members("cheap")` (the static pair).

## Tests (`test_cheap_tier.py`, 5 new, red before the change)
- The start's failing members are added in full-panel order.
- gmsh_prod stays when the start passes everything.
- No member is invented, and configs are identical to the full panel's.
- The search's cheap verdict uses the start's failures.
- Staged transactions judge with the members they are given.

The engine-arms test fakes of `stages.run_staged` now accept the new keyword. The cheap/full agreement-cell rule from B8 already counts a cheap pass as every cheap member passing.

## Gates
- **Focused:** 247 passed (`test_cheap_tier`, `test_stages`, `test_search_wiring`, `test_engine_arms`, `test_engine_entry_exit`, `test_v4_arms`).
- **mypy:** clean on `search.py`, `stages.py`, `__init__.py`, `assemble.py`.
- **ruff:** clean on the changed files. `tests/unit/cad/repair_v4/test_engine_arms.py` carries an I001 import-order finding that was **already on the tip** (a relative import); my change there is two lines in fakes.
- **Unit `tests/unit/cad/repair_v4` + `tests/unit/dev` against base `0fa39fd5`:** base 2139 passed / 39 failed, head 2144 passed / the SAME 39 (host /tmp-path; `comm` empty both ways). The +5 are the new tests.
