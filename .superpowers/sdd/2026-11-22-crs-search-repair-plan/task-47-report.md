# Task 47 report: the search stops spending ops on proposals that cannot apply

Branch `sdd/task-47` from `77804dfd`. Code/test head `4c7f1850` (the commit carrying this report follows it). No model, no GCP spend beyond the gate VMs, no push, no merge, no `pkill`.

## Headline
| | before (77804dfd) | after |
|---|---|---|
| 3-hole part, ops to `clean` | **70** (11 expansions, 40 verdict candidates) | **14** (4 expansions, 11 verdict candidates); 13 -> `ops`, 12 -> `ops` |
| ops per expansion | 6.4 | **3.5** |
| declines + duplicates among spent ops | **50 of 70 (71%)**: 20 make_volume declined, 10 sew declined, 14 orient_and_close duplicate, 6 fill_loop duplicate | **4 of 14 (29%)**: 3 sew declined, 1 orient_and_close no-op |
| Task 42 one-hole case | 8 ops, 5 verdict candidates | **5 ops, 4 verdict candidates**, `clean` (same at budget 6 and 8) |

**The one-hole case drops below 6.** Production `CAD_REPAIR_MAX_OPS = 6` now finishes it (measured: budget 6 -> `clean` at 5 ops).
**The 3-hole part still needs 14 > 12 (e2e) > 6 (production).** Per your instruction I stopped here and did not raise a budget; this is the design finding (below).

## Design finding for the owner
The floor for 3 holes is 4 ops (3 fills + `make_volume`). We spend 14 because every expansion runs EVERY proposal, so each of the 4 expansions on the path pays for the siblings: per expansion, 1 `sew` (declined every time, 3 total) and 1 `orient_and_close` (3 evaluated into new orientation-only states never used + 1 no-op), plus the fills. Both remaining ops cannot be ruled out from the state without geometry or running the op (reasons below). So a 3-hole part at a budget of 6 is not reachable by trimming proposals alone; it needs either (a) a layer-1 query that answers "does any free edge have a partner within the sew window" and "is the shell's orientation already consistent" (would take 14 -> ~7-8), or (b) the search charging an expansion rather than each sibling op, which is a budget-policy change I did not make. What the evidence says `CAD_REPAIR_MAX_OPS` should be: **6 is enough for 1 hole (5 ops); 3 holes need 14; roughly `4 * holes + 2`** is the cost model fitted to 1 -> 5 and 3 -> 14. Not changed.

## What changed
1. `search.py`: heap entry is now `(-rank, -depth, order, rank, state)` (`_entry`). Of equal scores the DEEPER state is popped first. Equal score and equal depth: `order`, the search's monotonic counter, taken in the driver thread in proposal order (results are collected in submission order), so it is proposal order, FIFO, never completion order. A better score still beats depth (tested both ways).
2. `search.py` `DetGenerator.propose`: (a) dedupes identical `(op, targets, params)` before the `k` cut (the open site and the invalid/no_solids site both asked for the same `make_volume` and the same `orient_and_close` every expansion = 2 ops/expansion); (b) `_declines_in_advance`; (c) `skipped` counter and `notes` so each drop is on the record.
3. `sites_kernel.py`: `volume_possibility(shape)` rides on the existing `sites_free_loops` call (no extra worker call). `sites.py` stores it on open sites as `graph["volume"]`; absent means unknown and the generator proposes.

## Decline classes: eliminated
- **make_volume with no closable shell.** Kernel-side topology fact: a closed shell uses every non-degenerate edge at least twice, so iteratively drop faces that have an edge used by fewer than two remaining faces; if nothing remains no subset can enclose a volume. `loose` (faces outside solids = what empty-targets `make_volume` uses) vs `any` (superset of any explicit target list). 20 of 70 ops before.
- **Duplicate candidates** (same op, targets, params): 2 per expansion.
- **sew on a state that carries a solid** (`B` id in the workspace): the op declines this outright. (Did not occur in the 3-hole run; guarded and tested.)
- **sew with zero free edges** (topological count 0; the op's own measure cannot exceed it): guarded and tested.
- Already eliminated before this task and re-checked: sew rungs / remove_small widths outside the kernel window (`scaled_*`), fill_loop over the 5% cap.

## Decline classes: deliberately left, and why
- **sew "no rung improved the shape"** (3 ops here). Whether a free edge has a partner within <= 0.1 mm is geometry the kernel discovers. A necessary-condition proximity test is not sound by construction: edges sharing a vertex are distance 0, and a collinear overlap of adjacent edges can in principle be sewn. Suppressing it could lose a real repair; I did not.
- **orient_and_close with no solid** (4 ops). Duplicates (no-op) and new orientation-only states both occur; `ShapeFix_Shell.Perform` can also touch faces, so "already consistent" is not a proof of no-op. Left.
- **fill_loop duplicates by commuted order** (6 ops before; 0 on the new path since depth-first never expands the siblings). Pruning by canonical order needs "fills of disjoint loops commute", true in practice but unproved; with depth-first it is moot.
- **make_volume when the topology test says "possible"** and the kernel finds none (e.g. closed box plus an unrelated hole elsewhere): kernel-discovered, proposed.
- fill deviation / MakerVolume results the kernel computes: proposed.

## Tie-break on equal depth
Equal score, equal depth -> proposal order (earlier pushed first). It does not depend on which worker finishes first: pushes happen in the driver thread over results gathered in submission order. A unit test with an op whose first candidate takes longest (120/60/0 ms, 4 workers, 4 runs) pins one expansion order.

## Tests
- Unit `tests/unit/cad/repair_v4/test_search_waste.py` (18): deeper tie first; equal depth FIFO; better score beats depth (two shapes); completion-order independence; deep tie chain cost (7 ops vs 15 breadth-first); a stuck search (strictly worse states, two branches) stops on patience with <30 ops of 1000; sweep tests including every "not suppressed" direction (facts absent, `any` vs `loose` for targeted make_volume, sew with free edges kept, fills/orient kept, dedupe before the `k` cut).
- Native `tests/native/test_native_repair_v4_waste.py` (6), real kernel in-process on 11 shapes (closed loose shell, missing face, six unsewn faces, closed box + flap, seam cylinder, cylinder missing a cap, solid, closed box beside an open one, flush-patch solid, 3-hole part, a 0.002 mm-gapped shell): predictor never says "impossible" where `make_volume` succeeds (4 shapes succeed, including two with free edges present, so the naive "free edge => decline" rule would fail), and says "impossible" for 6 the kernel declines (so "always possible" would fail); free_edges counts; sew succeeds on the gapped shell despite free edges; sew declines a solid. 3-hole at 14 ops -> `clean` with spent == 14, no `make_volume` decline, no fill duplicate, <= 4 wasted, <= 3.5 candidates per expansion; 13 -> `ops`.
- `test_native_repair_v4_patience.py` updated: grant 80 -> 14; one-hole pinned (5 ops, 4 verdict candidates).

## Mutations (each restored, tree clean after)
| mutation | unit (waste+patience+search) | native (waste file) |
|---|---|---|
| M1 no depth tiebreak | 2 fail (deeper-first, deep chain) | 3-hole cost test fails |
| M2 shallower first | same 2 fail | 3-hole cost test fails |
| M3 no dedupe | 4 fail | fails (cost) |
| M4 no make_volume prune | 4 fail | fails |
| M5 make_volume always suppressed when facts exist | 3 fail | **passes** (the e2e final state has no facts, so it is proposed); unit catches it |
| M6 targeted make_volume judged on `loose` | 1 fail | passes; unit catches it |
| M7 no sew solid guard | 1 fail | passes; unit catches it |
| M8 no sew free-edge guard | 3 fail | passes; unit catches it |
| M9 sew suppressed whenever facts known | 2 fail | 2 fail |
| M10 kernel predictor never closable | 53 pass (**unit blind**) | predictor test fails |
| M11 kernel predictor always closable | 53 pass (unit blind) | predictor test + 3-hole fail |
| M12 volume facts not attached | 53 pass (unit blind) | 3-hole cost test fails |
M1-M3 ran on gate-1 (before it self-deleted), M4/M5 on the host (stopped when you redirected), M6-M12 on gate-2. Honest gaps: M5-M8 are caught only by the unit tier (the native fixtures cannot reach a repair that over-suppression would lose); M10-M12 only by the native tier. Neither tier alone guards everything.

## Gates (clean clones, no `v4-` in path, absolute PYTHONPATH, gate-2). Head `4c7f1850`-equivalent code vs base `77804dfd`
- Unit (`tests/unit/cad/repair_v4 tests/unit/dev`): head 8 failed / 1889 passed; base 8 failed / 1871 passed. `comm -23 head base` (head-only): EMPTY. `comm -13 head base` (base-only): EMPTY. The 8: the known `test_the_settings_tier_passes_with_every_required_setting_hostile` plus 7 in `tests/unit/dev` (`test_v4_arms_launch`, `test_v4_fleet_launcher`, `test_v4_wave_controls`) that fail identically on base (environmental on the VM, not mine). +18 passes are my tests. **Proves little**: both sides carry the same failures.
- Native (`tests/native/test_native_repair_v4_*.py tests/native/test_native_v4_inject.py`): head 1 failed / 260 passed; base 1 failed / 254 passed. The failure on both: `test_repair_v4_is_callable_from_a_script_without_a_main_guard` (known). Both `comm` directions EMPTY. +6 passes are mine. Proves little for the same reason.
- ruff (6 changed files): first run flagged one import order in my new native test; fixed; rerun: all checks passed. mypy (search.py, sites.py, sites_kernel.py): first run caught a real narrowing error in sites.py (`dict | None` indexed); fixed; rerun: "Success: no issues found in 3 source files". The post-fix 9 native + 18 unit tests pass.
- Exit codes: every pytest was redirected to a file and `$?` echoed separately (gate script `unit/native gh/gb rc=1` = known failures only; mutation runs rc as listed above). The first lint attempt `rc=2` was a container cache-permission error, not a finding; reran with `--no-cache`.

## Side findings
- `sew` reports "input has no free edges" on a compound of LOOSE faces (no shell) even with 24 topological free edges, because `ShapeAnalysis_FreeBounds` measures shells. The generator proposes sew there and it declines. Possibly an op bug (the declined message says "Sew only open shells or loose faces"); not touched.
- The 3-hole run logs `kernel call v4_fix_poles: error (ShapeFix_Face.Perform changed nothing ...)` on both before and after; unrelated noise.
- gate-1 self-deleted mid-mutation (M4 hit "failed to connect"); results on it were all copied into this report; I touched `/var/run/gatevm-keep` on gate-2.

## Files
`src/meshpipeline/cad/repair_v4/search.py`, `sites.py`, `sites_kernel.py`; tests `tests/unit/cad/repair_v4/test_search_waste.py`, `tests/native/test_native_repair_v4_waste.py`, `tests/native/test_native_repair_v4_patience.py`.
