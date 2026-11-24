# A13 bisect: why 3596803faa stopped being a file the arms fail

Question from LEAD-2: the oracle pilot called `3596803faa` a file every arm fails, and on today's tip the det arm
repairs it in 11 ops. Which merge changed that - and did it change BY DESIGN or BY ACCIDENT?

Answer: **by design, in three steps, each merged on its own measured reasoning.** Two of them made the fix reachable
and rankable; the third made it fast enough to finish inside the wall the wave gave it.

## Method

One file, one arm, one budget at every point: `ARM=search_det`, `repair_v4` exactly as the wave calls it, budget
`wall_s=900, ops=60, model_calls=0`, deadline 1200 s - so the only variable is the code. Each point is a clean
`git archive` of that commit, run in the mesh image on `jig`. Timings are a single run each, on a shared host, so
treat small differences as noise and only the order-of-magnitude steps as real.

## The bisect

| point | commit | stop | clean | ops | search wall | the ops it evaluated |
|---|---|---|---|---|---|---|
| before C5 | `b396d645e~1` | `exhausted` | no | **0** | 0.001 s | nothing: no site, so nothing proposed |
| at C5 (located sites) | `b396d645e` | `patience` | no | 6 | 183 s | `split_angle` 90, 60, 45 - all evaluated, none kept |
| at B12 (caps removed) | `5c43220a4` | **clean** | **yes** | 8 | 827 s | 3x `split_by_proximity`, then 60, then `defeature` |
| at D16 (defeature ordered) | `370662893` | clean | yes | 9 | 779 s | same shape |
| at A11.2 (aimed contacts) | `e2b25ddf6` | clean | yes | 9 | **110.8 s** | `split_angle` 60 first |
| at D18 (aimed slivers) | `4c74661eb` | clean | yes | 9 | 111.2 s | `split_angle` 60 first |
| today | `8d193d20a` | clean | yes | 11 | 101 s | `split_angle` 60 first |

Three steps, each visible as a change in kind rather than degree:

1. **C5's located sites made the fix REACHABLE.** Before C5 the arm proposed nothing at all - the member's failure
   was unlocated, so no site existed and the sweep had nothing to sweep. After C5 the file lands at one `proximity`
   site and the det sweep proposes `split_angle` at 90, 60 and 45 there. (Note for the permission hypothesis:
   `proximity` is a kind det's gate already allows. This file never needed a gate opened.)
2. **The owner's clean definition made the repaired state RANK.** At C5 the winning op is proposed AND evaluated and
   the run still ends with the input as its best state. By B12 - clean = valid + panel, the fidelity caps deleted -
   the same state outranks the input and the run ends clean. A split moves the part, so under the old rule the
   repaired state could not win.
3. **A11.2's aiming made it FAST ENOUGH.** 779 s at D16, 110.8 s at A11.2, and D18 within noise of that.

## The control, because step 3 is my own commit

Two attributions tested by reverting in a scratch tree at today's tip, not by reading the ordering:

- **My frontier fix: REJECTED.** Reverting it (score keys 3 and 4 ranking invalid states again) leaves the file
  clean in 101 s, same path, same 11 ops. It is not what made this file work.
- **My A11.2 proximity aiming: CONFIRMED.** Reverting only that one line - the sweep back to
  `itertools.combinations` instead of the kernel's measured clashes - takes the same file from 101 s to
  **1230 s**, 12x, with the three `split_by_proximity` evaluations back in front of `split_angle` 60.

## Why this mattered to the wave, and to the sweep's premise

1230 s of search is **past the approved 900 s search wall** (`sdd/fast-loop`: 1200 s per file, 900 s search). So on
pre-A11.2 code this file is not unreachable - it is too slow, and the wall cuts it before it finishes. That is the
honest reading of "the arms fail this file": it was TRUE at the tip the wave ran, for a reason that was never about
the op library or the site taxonomy. The combinatorial proximity sweep spent the budget on pairs nothing had
measured, and the file ran out of wall while holding the answer.

The implication for the oracle sweep is the same one v4 is now the guard for: a file list built from an older tip
measures what the search could not reach LAST WEEK. v4 runs all four arms over the same 150 files on today's tip, so
pass 2 can be gated on its rows.

## A harness bug of mine, found mid-bisect and fixed

One point came back `NO_OUTPUT` and I nearly recorded it as a datum. The cause: I was given the A11.2 merge as
`e2b25ddf` and used `e2b25ddf0`, which is not a valid object. `git archive` failed, `tar` extracted nothing, the
container ran an empty tree, and the probe printed `NO_OUTPUT` as though that were a measurement - the fourth
instance tonight of a step that looked fine and was not (the `*.ste*` glob, the unbuffered `grep`, the shell eating
a message's backticks). The bisect harness now verifies the sha resolves, asserts the extracted tree actually
contains the engine, surfaces the container's stderr instead of discarding it, and refuses an empty result.

## Raw evidence, on `jig`

- `/srv/work/lane-a-out/gate_before.json` - the gate check over three parts: which site kinds each file lands at,
  and which kind-gated face ops the det sweep proposes there.
- `/srv/work/lane-a-out/kcut.json` - the k-cut trace: per expansion, the full uncapped proposal list and where
  `split_angle` 60 ranks in it, with whether it survived the arm's own k.
