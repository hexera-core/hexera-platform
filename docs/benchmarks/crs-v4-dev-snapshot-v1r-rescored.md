# CRS v4 dev snapshot v1r: RE-SCORE under clean = valid + panel (PROVISIONAL, WAVE STILL RUNNING)

**A RE-SCORE of a run in flight, not a re-run.** Source rows: `gs://dev-crsv3-transfer-224734058693/eval/snap-v1r-202611240237/partial-sync/out/` (wave `snap_v1r_dev` / `snap_v1r_clean`,
commit `a9dae3bb`). Every number comes from a layer those rows already recorded; no checker, mesher or model ran.

The commit matters: `a9dae3bb` PREDATES the definition change, so these rows' stored `out_clean` was written under
the old rule (valid + panel + faithful). The re-score is therefore a real test of the new rule, not a tautology.

## The number the owner is waiting for: 0 of 73 conclusive rows change verdict

Covered so far: **80 rows** - 44 streamed from the 6 dev shards (all still running) and 36 from the clean control
(finished). 73 conclusive, 7 inconclusive.

**Not one row's verdict moves.** The reason is the same one v1 showed, and it is now measured twice: 23 conclusive
rows DID fail layer 3, and **all 23 also failed layer 1 or the panel**, so the fidelity gate was never what kept
them from clean. Zero rows are valid-with-the-whole-panel-passing-but-unfaithful, which is the only class the
decision can move. Not one row is even within one panel member of that class.

So on the evidence so far the new goal does not change v1r's numbers either, and the AI marginal it measures stands
or falls on layer 1 and the meshers alone.

**Caveats.** (1) The dev wave is ~2-2.5 h from finishing and has published 44 of its rows; the counts below will
grow and this conclusion could still change, which is why the table says PROVISIONAL. (2) The gaming review
(volume / bbox) cannot be computed for this run: `a9dae3bb` predates those row fields, so every clean row is counted
as unmeasured rather than assumed within the limits.

Refresh: `rescore.py --from <dir> --title ... --md ...` over the same prefix once the shards finish.

---

## v1r dev split (in flight)

**PROVISIONAL: the wave is still running.** 6 shard(s) are streaming their rows (shard-1-of-6, shard-2-of-6, shard-3-of-6, shard-4-of-6, shard-5-of-6, shard-6-of-6), so the counts below cover only the files those shards have finished so far and will grow. Re-run this when the wave completes.

**This is a RE-SCORE of an existing run, not a re-run.** Source rows: `gs://dev-crsv3-transfer-224734058693/eval/snap-v1r-202611240237/partial-sync/out/snap-v1r-dev-202611240237`. Every number below comes from a layer those rows already recorded (44 rows); no checker, mesher or model ran, and no geometry was measured again.

The definition changed on 2026-11-24 (owner): **clean = layer 1 valid + layer 2 panel**. Layer 3 (faithfulness) is still measured and reported, and no longer gates. `old` below is the number the run published under the previous rule (valid + panel + faithful); `new` is the same rows under the current one.

| arm | files | conclusive | clean old | clean new | clean in (new) | gain old | gain new |
|---|---|---|---|---|---|---|---|
| input_only | 14 | 14 | 0 | **0** | 0 | 0 | **0** |
| search_ai | 8 | 5 | 0 | **0** | 0 | 0 | **0** |
| search_det | 10 | 6 | 0 | **0** | 0 | 0 | **0** |
| staged | 12 | 12 | 0 | **0** | 0 | 0 | **0** |

## Gaming review (volume / bbox)

Not available for this run: rows written before 2026-11-24 carry no volume or bbox ratio, so the review list cannot be rebuilt for them; they are counted as unmeasured, not as within the limits.

---

## v1r clean control (finished)

**This is a RE-SCORE of an existing run, not a re-run.** Source rows: `gs://dev-crsv3-transfer-224734058693/eval/snap-v1r-202611240237/partial-sync/out/snap-v1r-clean-202611240237`. Every number below comes from a layer those rows already recorded (36 rows); no checker, mesher or model ran, and no geometry was measured again.

The definition changed on 2026-11-24 (owner): **clean = layer 1 valid + layer 2 panel**. Layer 3 (faithfulness) is still measured and reported, and no longer gates. `old` below is the number the run published under the previous rule (valid + panel + faithful); `new` is the same rows under the current one.

| arm | files | conclusive | clean old | clean new | clean in (new) | gain old | gain new |
|---|---|---|---|---|---|---|---|
| input_only | 9 | 9 | 9 | **9** | 9 | 0 | **0** |
| search_ai | 9 | 9 | 9 | **9** | 9 | 0 | **0** |
| search_det | 9 | 9 | 9 | **9** | 9 | 0 | **0** |
| staged | 9 | 9 | 9 | **9** | 9 | 0 | **0** |

## Gaming review (volume / bbox)

Not available for this run: rows written before 2026-11-24 carry no volume or bbox ratio, so the review list cannot be rebuilt for them; they are counted as unmeasured, not as within the limits.
