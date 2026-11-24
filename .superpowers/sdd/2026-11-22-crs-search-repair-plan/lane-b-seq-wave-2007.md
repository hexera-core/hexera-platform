# search_seq on the 4-arm wave (202611242007): the headline holds, and the arm is losing on REACH

Rows: `/Users/kitts/archductor/snap-runs/fast-202611242007/out/merged-{dev,clean}`. Analyser:
`lane-b24/analyse_seq.py`. Timings are comparable only within this wave (it carries A16/A17, C16's caps,
C18 P1 and D25's parallel healer panels).

## 1. The per-arm table

| arm | rows | incl | clean | mean wall | full/file | incv | local/file | $ |
|---|---|---|---|---|---|---|---|---|
| search_ai | 18 | 0 | 8 | 406 s | - | - | - | 3.23 |
| search_det | 17 | 0 | 7 | 330 s | - | - | - | 0 |
| search_seq | 18 | 0 | 6 | 189 s | **1.11** | **0** | 0.72 | 0 |
| search_seq_ai | 18 | 0 | 6 | 193 s | **1.11** | **0** | 0.72 | 0.04 |

Dev only: det 2/12, ai 3/13, seq 1/13, seq_ai 1/13. Clean controls 5/5 for all four arms.

**The headline is real: 1.11 full panels per file, zero inconclusive verdicks, against 0.72 local checks.**
The arm costs about a third of the frontier arms' wall (189 s against 330 and 406) and pays one panel per
file. That part of the design works exactly as the owner specified.

## 2. What the arm did with its 27 defects

Found: `overlapping_facets` 9, `low_quality` 10, `other` 5, `body_overlap` 2, `open_shell` 1.
**Fixed: 3, all `low_quality`, all by `split_by_proximity`.** Skipped: 24.

Of 72 steps, **52 never applied at all**, 5 had no rule, and only **15 reached a local check** (9 "still
there", 3 "gone", 1 "new defect", 2 partial clusters).

| defect kind | steps that never applied | no rule | reached a check |
|---|---|---|---|
| overlapping_facets | 19 | 0 | 8 |
| low_quality | 15 | 0 | 5 |
| body_overlap | 15 | 0 | 2 |
| open_shell | 3 | 0 | 0 |
| other | 0 | 5 | 0 |

**So the answer to "reach or power" is REACH, and overwhelmingly.** And power is unproven rather than
disproven: on every dev file except the one all four arms cleaned, seq's delivered panel count equals the
INPUT's, so the three local passes bought no whole-part gain on this sample.

## 3. The top cause is a BUG OF MINE: body defects carry layer-1 names, not workspace ids

18 of the 52 non-applications read `none of the defect's entities are live any more`, and **not one of them
follows a kept repair** - they fire on the first step, with nothing changed yet. The trace says why:

```
body_overlap on S1+S4 -> conformalise [table: the top rule] -> not applied: none of the defect's entities are live any more
```

`S1`, `S4` are **layer 1's** solid indices (`valid_kernel` emits `f"S{i+1}"`). The workspace's stable ids
for bodies are `B...` (lane C's `local_verify._live` filters on that prefix). My `defects_of` builds a body
defect straight from the layer-1 finding's entities, so every body defect carries ids the workspace has
never heard of - and B26's own live-id filter, correctly, refuses to derive parameters from them.

Consequence: **every `body_overlap` and `open_shell` repair in this wave was dead on arrival** - 18 of 52
non-applications, both body_overlap files, including `00518960` which sits at 0/4. The cluster code,
the per-pair clash derivation and lane C's per-pair verification were never exercised on a real part.

Second cause, 10 steps: `reapproximate` declines `no_valid_face: the new carrier does not give a valid face`
on real geometry. It is the table's top rule for `low_quality`, and it never applied once.

Third, 13 steps: `remove_small` declining `no_change: nothing at or under <X> mm to remove`, where X is
derived from the narrowest located face. On big parts X came out as 1211 mm and 974 mm, which says the
derivation is picking a face whose own measure the op disagrees with.

## 4. The AI pick WAS reached - 6 times on 3 files, and it changed nothing measurable

| file | asked | chose | why, in its own words |
|---|---|---|---|
| 0051_00512819 | 1 | `fill_loop + remove_small` | "an open shell with no overlaps or clashes, so filling a loop most directly..." |
| 0051_00518960 | 3 | `fuse` (x3) | "the three measured overlap pairs indicate overlapping bodies, which fuse directly resolves" |
| 0057_00574915 | 2 | `conformalise` (x2) | "all measured clash depths are zero, indicating touching interfaces rather than volumetric overlap" |

Answered 6 of 6, nothing ignored, no failed rounds, **$0.04 for the wave**. The picks are sensible on their
face - the `00574915` one reads the zero clash depths and chooses conformal contact over a fuse, which is
the right reading of that measurement - and on `00518960` it chose `fuse` where the table leads with
`conformalise`, so it did diverge from the table's order.

**But none of it could land: all 6 picks are on body defects, which are exactly the ones killed by the id
bug above.** The two arms' defect counters are identical, and the three files where the steps differ end on
the same panel count. So `search_seq_ai` is not yet a measurement of the AI pick, and its equal clean count
(1 and 1) says nothing about the model either way.

## 5. The demotion proposal, widened - and NOT the first thing to do

The cheap-vs-full rollback I flagged before the wave did happen, and exactly once:

| rolled back | of tried | applied | defect kind -> repair |
|---|---|---|---|
| 1 | 6 | 0 | `overlapping_facets` -> `split_angle + run_stage:condition` |

One step of 72, one kind. The mechanism is real and worth fixing, but on this evidence it is not what the
arm is losing to, and a demotion keyed only on the stage's cheap veto would move one entry.

**The same mechanism answers a much bigger share if it keys on "did not apply" generally.** The table's top
entry for a kind should be demoted below its alternatives when the wave's own traces show it does not apply
there, whatever the cause - a stage rollback on the cheap score (1), an op that declines on real geometry
(`reapproximate` for `low_quality`, 10), or a derived parameter the op refuses (13). That is the same rule
LEAD-3 approved, with the trigger broadened from one cause to the measured outcome, and it is learned from
the arm's own traces rather than from an oracle that never saw any of these refusals.

**Order I recommend, and why:**

1. **The body-id translation.** It is a one-place fix of my own bug and it unblocks 18 of 52 non-
   applications plus every cluster and AI-pick path. Nothing else can be measured honestly until it lands:
   with it broken, `body_overlap` and `open_shell` have never been tried.
2. **Re-run the fast loop.** Then the reach numbers mean something and the demotion has real counts behind
   it for every kind, not just the one that rolled back.
3. **Then the demotion**, on those counts, by measured outcome.

Doing (3) before (1) would rank a table against a wave in which a third of its entries could not run.
