# Lane B18: the search is anytime, so a wall no longer erases its result

Design 5.7 ("output the best state so far"), owner ruling 2026-11-24. Merged at `f885bd4f`.

## The evidence this is built on (B16, on v3b)

| file | search_det (KEPT) | search_ai (DISCARDED) | the AI's best state |
|---|---|---|---|
| `bcf201b55323` | exhausted in **60 s**, 6 ops, panel **2**→2 | wall at **668 s**, 38 ops, $0.45, inconclusive | **panel 3**, 5-state path |

The only reason that row was deleted is one clause in `measurement.file_unmeasured`:

```python
or (reason in TIME_STOPS and not out_clean)
```

It was right about machine time and wrong about the verdict. The best state's FULL verdict was already in the
trace (its `confirm` event), and `deliverable` already refuses to ship anything worse than the input on
valid + panel. The effect was an asymmetry nobody chose: **an arm that explores is structurally more likely to be
deleted from the measurement than an arm that gives up early.** det exhausted its candidates in 50–60 s and was
counted; the AI worked for 650–750 s, reached states det never reached, and was not.

## What changed

- **`search.py`: `VERDICT_RESERVE = 0.2`.** Proposing stops at `1 - reserve` of `Budget.wall_s`; the rest is the
  VERDICT BUDGET. The search then ensures its best state carries a full-panel verdict and delivers it.
  **The reserve comes out of the existing wall, never added to it** — no arm gets more wall-clock than it had, so
  v3c's numbers stay comparable with the next wave's.
- **`SearchResult`** carries `stopped_on_wall`, `best_tier` and `final_verdict` (what the reserve paid for, when it
  was needed at all).
- **`measurement.file_unmeasured`** takes `out_tier`. A time stop is unmeasured **only** when even the final verdict
  did not complete. One rule, both callers, as its own docstring requires: the search's `unmeasured`, the row's
  `inconclusive` and the report's contradiction check all read it.
- **`arms.py`** writes `stopped_on_wall` on the row. **`report.py`** carries the field, keys its contradiction check
  on the tier instead of the stop reason, and reports wall-stopped rows **per arm** with the clean rate *without*
  them, so a reader can drop them from a denominator without taking anything on trust.

## One thing I found while building it, stated plainly

**The reserve is a guarantee, not the fix.** In today's `search`, a child is only promoted to `best` *after* the
confirm step has judged it with the full panel, so the delivered state is **already** full-tier however the search
ended — I pinned that with a test across every stop reason. The thing that was deleting rows was the rule, not a
missing verdict. The reserve still earns its place: it makes the invariant true by construction rather than by
property, it leaves time for the final verdict if confirmation is ever changed to promote on cheap scores, and it
stops proposing earlier, which shortens the soft-wall overshoot (D15 measured up to +1406 s past a 600 s wall).
`final_verdict` is `None` in the normal case and says so.

## The same rule for every arm

What counts is the tier of the verdict delivered, not how the search ended:

| stop | out_tier | measured? |
|---|---|---|
| clean / exhausted / patience / ops | full | yes (unchanged) |
| **wall** | **full** | **yes — this is the change** |
| wall | cheap, or absent | no: the one case the ruling leaves unmeasured |

## What this does to the existing docs — read this before comparing any numbers

Every wave so far **discarded** wall-stopped searches. So **v1r's, v2's and v3c's search numbers are floors
measured under the old rule**, and the AI's especially, because it hits the wall more often: on v3b's dev rows,
5 of 10 `search_ai` rows were wall-stopped against 6 of 17 for `search_det`. They are not comparable with what
comes next.

Nothing is silently restated. A row written before the ruling carries no full-tier verdict on a time stop (an
inconclusive row carries no verdict at all), so `out_tier` is absent and those rows keep their old classification
automatically. **The first comparable AI marginal is the next wave's.**

## Gates

- `tests/unit/cad/repair_v4/test_search.py`: 31 pass, including the reserve measured against a pinned clock (a
  reserve of 0.2 stops at 8.5 s of a 10 s wall and a reserve of 0 does not), a wall-stopped search being conclusive
  and carrying its best state, the delivered tier being full whatever stopped the search, and the one unmeasured
  case.
- `tests/unit/dev/test_v4_arms.py`: 94 pass. The two tests that pinned the OLD rule are rewritten to the new one —
  a wall stop with a full verdict is conclusive, one without is still refused as a contradiction — plus a new test
  that the arm block counts wall-stopped rows apart and reports the clean rate without them.
- `tests/unit/cad/repair_v4 tests/unit/dev` against the feature tip: **42 failed / 2339 passed, the same 42 as
  base** (host `/tmp`-path and a macOS-only `sched_getaffinity` mypy ratchet, in files this change never touches).
- ruff and mypy clean on all four changed source files.

## Not in scope

Subtracting model latency from the wall (B16's second point) is not approved and is not here. For the record, the
numbers I was asked for separately: on v3b's wall-stopped AI rows, subtracting 10–30 s per model call would bring
3 of 5 back inside the 600 s wall, and would do nothing for det's 6 wall-stopped rows — which is the honest reason
to treat the wall, not the latency, as the thing that was wrong.
