# CRS v4 dev snapshot v1: RE-SCORE under clean = valid + panel

**A RE-SCORE of three existing runs, not a re-run.** The owner changed the definition on 2026-11-24: clean is now
layer 1 **valid** + layer 2 **panel**, and layer 3 (faithfulness) is measured and reported but never gates. These
tables re-apply that rule to the rows snapshot v1 already recorded. No checker, mesher or model ran; no geometry was
measured again; every number comes from a layer the rows carry (`out_valid`, `out_panel_passes`, `panel_size`, and
the same three for the input).

## The finding: v1's numbers do not move

**0 of 256 recorded rows change verdict under the new definition - none.** Every arm's clean count, clean gain
and regression count is identical old and new, across all three runs.

Why: 54 rows did fail layer 3, but **all 54 also failed layer 1 or the panel**, so the fidelity gate was never the
binding constraint in v1. Removing it therefore cannot explain, or fix, v1's AI marginal of 0 - that result stands
unchanged under the new goal. The definition change will only show up on runs where an output is valid and meshes on
every member while having moved the part, which is what the gaming flag (volume change over 25%, bbox shrink over
10%) now exists to surface.

Reproduce: `devtools/validation/v4_eval/rescore.py --from <results dir> --title ... --md ...`

---

## dev split (30 files), wave snap_v1_dev

**This is a RE-SCORE of an existing run, not a re-run.** Source rows: `.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1/results/snap-v1-dev (commit 79d7ba35)`. Every number below comes from a layer those rows already recorded (114 rows); no checker, mesher or model ran, and no geometry was measured again.

The definition changed on 2026-11-24 (owner): **clean = layer 1 valid + layer 2 panel**. Layer 3 (faithfulness) is still measured and reported, and no longer gates. `old` below is the number the run published under the previous rule (valid + panel + faithful); `new` is the same rows under the current one.

| arm | files | conclusive | clean old | clean new | clean in (new) | gain old | gain new |
|---|---|---|---|---|---|---|---|
| input_only | 30 | 30 | 1 | **1** | 1 | 0 | **0** |
| search_ai | 28 | 28 | 2 | **2** | 1 | 1 | **1** |
| search_det | 28 | 28 | 2 | **2** | 1 | 1 | **1** |
| staged | 28 | 28 | 2 | **2** | 1 | 1 | **1** |

## Gaming review (volume / bbox)

Not available for this run: rows written before 2026-11-24 carry no volume or bbox ratio, so the review list cannot be rebuilt for them; they are counted as unmeasured, not as within the limits.

---

## dev split minus the flipped file (29 files), wave snap_v1_dev

**This is a RE-SCORE of an existing run, not a re-run.** Source rows: `.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1/results/snap-v1-dev29 (commit 79d7ba35)`. Every number below comes from a layer those rows already recorded (110 rows); no checker, mesher or model ran, and no geometry was measured again.

The definition changed on 2026-11-24 (owner): **clean = layer 1 valid + layer 2 panel**. Layer 3 (faithfulness) is still measured and reported, and no longer gates. `old` below is the number the run published under the previous rule (valid + panel + faithful); `new` is the same rows under the current one.

| arm | files | conclusive | clean old | clean new | clean in (new) | gain old | gain new |
|---|---|---|---|---|---|---|---|
| input_only | 29 | 29 | 0 | **0** | 0 | 0 | **0** |
| search_ai | 27 | 27 | 1 | **1** | 0 | 1 | **1** |
| search_det | 27 | 27 | 1 | **1** | 0 | 1 | **1** |
| staged | 27 | 27 | 1 | **1** | 0 | 1 | **1** |

## Gaming review (volume / bbox)

Not available for this run: rows written before 2026-11-24 carry no volume or bbox ratio, so the review list cannot be rebuilt for them; they are counted as unmeasured, not as within the limits.

---

## clean control (8 files), wave snap_v1_clean

**This is a RE-SCORE of an existing run, not a re-run.** Source rows: `.superpowers/sdd/2026-11-22-crs-search-repair-plan/snap-v1/results/snap-v1-clean (commit 79d7ba35)`. Every number below comes from a layer those rows already recorded (32 rows); no checker, mesher or model ran, and no geometry was measured again.

The definition changed on 2026-11-24 (owner): **clean = layer 1 valid + layer 2 panel**. Layer 3 (faithfulness) is still measured and reported, and no longer gates. `old` below is the number the run published under the previous rule (valid + panel + faithful); `new` is the same rows under the current one.

| arm | files | conclusive | clean old | clean new | clean in (new) | gain old | gain new |
|---|---|---|---|---|---|---|---|
| input_only | 8 | 8 | 8 | **8** | 8 | 0 | **0** |
| search_ai | 8 | 8 | 8 | **8** | 8 | 0 | **0** |
| search_det | 8 | 8 | 8 | **8** | 8 | 0 | **0** |
| staged | 8 | 8 | 7 | **7** | 8 | -1 | **-1** |

## Gaming review (volume / bbox)

Not available for this run: rows written before 2026-11-24 carry no volume or bbox ratio, so the review list cannot be rebuilt for them; they are counted as unmeasured, not as within the limits.
