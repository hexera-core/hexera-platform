# Lane B: the fast run on 2941d7bc6 (B19 + B20 + B21), read against the walls

Rows: `/Users/kitts/archductor/snap-runs/fast-202611241255/out/merged-dev/*/rows.json`.
Delta: `/Users/kitts/archductor/snap-runs/fast/delta-b21-202611241255.md`. Warm shared verdict cache.

## B20, the one number that is cache-independent

**165 verdicts saved against 219 spent across 20 rows: 43.0% of the verdict work removed.** A pruned
candidate costs neither a verdict nor an applied op, so this figure does not move with cache warmth.

| rule | pruned |
|---|---|
| `already_run_on_this_lineage` | 153 (93%) |
| `no_change_on_these_targets_already` | 12 (7%) |

The duplicate rule did nearly all of it. So the waste was not the AI guessing badly — it was the det
sweep re-offering identical rows down a lineage, which is what you would expect once B19 removed the
kind gates.

### Per file (saved / spent)

| file | saved % |
|---|---|
| `8c6a3f2186e7` det 67/18 | 79% |
| `7917f3122cae` | 44–50% |
| `de9ad40118a4` | 48–53% |
| `263d5e2d5bab` | 39–48% |
| `f3819472cf82` | 24–29% |
| `4c1fc7d5579d` | ~2/2 |
| `722c122c9b80` | 0–11% |
| `695d74ca7fa6` | **0 saved / 3 spent, in 882 s of search** |
| clean files | 0 — they reach clean before the sweep repeats itself |

## The ceiling moved, and not to where I said it was

219 verdicts over 20 rows at 500–900 s of search each is **45–90 s per verdict even warm**.
`695d74ca7fa6` is ~294 s per verdict: it spent its whole wall on three panels and had nothing to
prune because it never reached a second expansion. That is panel cost — A15's territory — not
candidate waste.

**I withdraw my `k_propose` hypothesis.** Raising k buys more candidates against a budget now spent
inside the verdict. Lowering it is not the lever either: B20 already drops the duplicates for free,
before any verdict is paid for.

## B21: the chains reached the front, and this sample cannot judge them

det proposed `run_stage` rows on `263d5e2d5bab`, `7917f3122cae` and `de9ad40118a4`. Their fates:

| row | outcome |
|---|---|
| `run_stage:bodies` | declined — "stage 'bodies' changed nothing: conformalise: declined: no_change" |
| `run_stage:condition` | declined — "was rolled back: it lowered the cheap score" |
| `run_stage:shells` | duplicate |

Only `de9ad40118a4` search_ai evaluated any (1 bodies, 1 condition).

Then the question that matters: are these even the oracle's chain files? Of this sample's 8 not-clean
files, **4 appear anywhere in lane D's table, and exactly one (`4c1fc7d5579d`) is a file where
`defeature → run_stage` won** — same for `remove_small → run_stage` and `remove_small → split_angle`;
`fuse → conformalise`, none. The chains fired on files the table never learned from, so their
declining says nothing about the table. **B21 is still unmeasured**, and LEAD-3's note stands: the
held-out run is the honest test.

### One mechanism worth carrying into A15

"rolled back: it lowered the cheap score" means the stage applied and the inner loop rejected it **on
the cheap tier**. The oracle judged on the FULL verdict. A stage that lowers the cheap score and
raises the full one is a win the oracle can see and the arms structurally cannot — the same
cheap-vs-full trap that bit my B13 table and lane A's frontier bug. If any of the oracle's 12
`defeature → run_stage` wins are of that shape, B21's table can be perfect and the chain still never
lands.

## The rest of the run

- clean: det 7, ai 7 — unchanged.
- regressions 2 → 0; excluded 9/8 → 5/5, with 7 rows moving excluded → valid-not-clean (1/4 panel).
- stops per arm: clean 7, patience 2, wall 6; `stopped_on_wall` 6.
- 5 files still excluded on `file_timeout` at 1200 s.

**I cannot separate those improvements from the warm shared cache in this run.** The honest reading:
B19+B20 cost no clean files and removed the regressions, and the 43% is the only part I would defend
without a cold control.

## What the next fast run should sync

Per-expansion counts for `263d5e2d5bab` need traces, which this run did not sync. With them I can say
which expansion the duplicates cluster in, which would tell us whether a cheaper fix is capping
lineage depth rather than re-ranking.
