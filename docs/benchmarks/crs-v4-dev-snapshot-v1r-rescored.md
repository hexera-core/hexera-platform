# CRS v4 dev snapshot v1r: RE-SCORE under clean = valid + panel (PROVISIONAL, WAVE STILL RUNNING)

**A RE-SCORE of a run in flight, not a re-run.** Source rows: `gs://dev-crsv3-transfer-224734058693/eval/snap-v1r-202611240237/partial-sync/out/` (waves `snap_v1r_dev` / `snap_v1r_clean`,
commit `a9dae3bb`). Every number comes from a layer those rows already recorded; no checker, mesher or model ran,
and no geometry was measured again. The work-state BREPs under `work-states/` were deliberately NOT read: recomputing
fidelity from them would be a re-measurement, which is not what a re-score is for.

The commit matters: `a9dae3bb` PREDATES the definition change, so these rows' stored `out_clean` was written under
the old rule (valid + panel + faithful). The re-score is therefore a real test of the new rule, not a tautology.

## The number the owner is waiting for: 0 of 111 conclusive rows change verdict

Covered so far: **122 rows** - 86 from the dev wave (5 of 6 shards final; only shard-5-of-6 is still
streaming) and 36 from the clean control (finished, exit 0). Conclusive: 75 dev + 36 clean.

**Not one row's verdict moves.** The reason is the same one v1 showed, now measured on two independent runs: the rows
that fail layer 3 ALSO fail layer 1 or the panel, so the fidelity gate was never what kept them from clean. Rows that
are valid with the whole panel passing but unfaithful - **the only class the decision can move - number zero**, and
not one row is within a single panel member of that class.

The fidelity gate was not what stopped these repairs. Layer 1 and the meshers are the whole game.

## This wave is NOT affected by the frontier regression; v2 IS

The search frontier was mis-ordered between `ac7fbf52` and `c96fd5836`: layer 3 was measured on the cheap tier the
inner loop ranks on, where an unfinished repair has no validity, panel or SICN signal, so `-max_dev_mm` became the
whole order and ranked the state that repaired LESS first (measured: the three-hole part stopped reaching clean at
all). This wave ran at **`a9dae3bb`, which predates `ac7fbf52`**, so its `search_det` / `search_ai` rows were
searched under the correct order and the counts above stand.

**v2 does not have that protection**: it launched at `5c43220a`, a descendant of `ac7fbf52`, so its search arms ran
under the mis-ordered frontier. Its search numbers are a floor, not a measurement of what the engine can do - the
symptom to look for in its traces is a stop reason of `patience` with the best state being the input.

## The gaming review CANNOT be produced for this run, and "unmeasured" does not mean "within the limits"

The volume and bbox ratios the review reads were added to the row by the same change that moved the definition
(`ac7fbf52`). This wave ran at `a9dae3bb`, before it, so **no row carries them** and every clean row is counted as
`unmeasured`. **That is not a statement that those outputs stayed inside the limits - nobody measured them.**
The same holds for **v2**, which also launched before `ac7fbf52`: it cannot produce a review list either. The first
wave that can is one launched at or after `ac7fbf52`.

**Still provisional**: 1 of 6 dev shards (shard-5-of-6) is still streaming, so the counts will grow and this conclusion could change.
Refresh: `rescore.py --from <dir> --title ... --md ...` over the same prefix.

---

## v1r dev split (in flight)

**PROVISIONAL: the wave is still running.** 1 shard(s) are streaming their rows (shard-5-of-6), so the counts below cover only the files those shards have finished so far and will grow. Re-run this when the wave completes.

**This is a RE-SCORE of an existing run, not a re-run.** Source rows: `gs://dev-crsv3-transfer-224734058693/eval/snap-v1r-202611240237/partial-sync/out/snap-v1r-dev-202611240237`. Every number below comes from a layer those rows already recorded (86 rows); no checker, mesher or model ran, and no geometry was measured again.

The definition changed on 2026-11-24 (owner): **clean = layer 1 valid + layer 2 panel**. Layer 3 (faithfulness) is still measured and reported, and no longer gates. `old` below is the number the run published under the previous rule (valid + panel + faithful); `new` is the same rows under the current one.

| arm | files | conclusive | excluded | clean old | clean new | clean in (new) | gain old | gain new |
|---|---|---|---|---|---|---|---|---|
| input_only | 24 | 24 | 0 | 0 | **0** | 0 | 0 | **0** |
| search_ai | 20 | 14 | 3 | 2 | **2** | 0 | 2 | **2** |
| search_det | 20 | 15 | 3 | 2 | **2** | 0 | 2 | **2** |
| staged | 22 | 22 | 2 | 0 | **0** | 0 | 0 | **0** |

## Files that contribute to no arm comparison

Every arm missing below was EXCLUDED, so this is final: no arm pair shares these files and they cannot move a marginal either way.

- `abc/0052_00528484_58a256af264a260f6f931a1c_step_003.step` (abc_fail): measured on input_only, staged; excluded on search_ai, search_det (file_timeout: file wall clock 1800s exceeded)
- `occt/bug32314_corrupted.stp` (occt): measured on input_only; excluded on search_ai, search_det, staged (file_timeout: file wall clock 1800s exceeded)
- `NIST-PMI-STEP-Files/nist_ftc_07_asme1_ap242-e2.stp` (nist): measured on input_only; excluded on search_ai, search_det, staged (file_timeout: file wall clock 1800s exceeded)

## Files an arm has not reached yet (the wave is still running)

These are NOT losses: the arm has no row because its shard has not finished the file.

- `abc/0057_00570551_59995fb3cd27b48fe0957ae2_step_007.step`: measured on input_only, staged; waiting on search_ai, search_det

## Gaming review (volume / bbox)

Not available for this run: rows written before 2026-11-24 carry no volume or bbox ratio, so the review list cannot be rebuilt for them; they are counted as unmeasured, not as within the limits.

---

## v1r clean control (finished)

**This is a RE-SCORE of an existing run, not a re-run.** Source rows: `gs://dev-crsv3-transfer-224734058693/eval/snap-v1r-202611240237/partial-sync/out/snap-v1r-clean-202611240237`. Every number below comes from a layer those rows already recorded (36 rows); no checker, mesher or model ran, and no geometry was measured again.

The definition changed on 2026-11-24 (owner): **clean = layer 1 valid + layer 2 panel**. Layer 3 (faithfulness) is still measured and reported, and no longer gates. `old` below is the number the run published under the previous rule (valid + panel + faithful); `new` is the same rows under the current one.

| arm | files | conclusive | excluded | clean old | clean new | clean in (new) | gain old | gain new |
|---|---|---|---|---|---|---|---|---|
| input_only | 9 | 9 | 0 | 9 | **9** | 9 | 0 | **0** |
| staged | 9 | 9 | 0 | 9 | **9** | 9 | 0 | **0** |
| search_det | 9 | 9 | 0 | 9 | **9** | 9 | 0 | **0** |
| search_ai | 9 | 9 | 0 | 9 | **9** | 9 | 0 | **0** |

## Gaming review (volume / bbox)

Not available for this run: rows written before 2026-11-24 carry no volume or bbox ratio, so the review list cannot be rebuilt for them; they are counted as unmeasured, not as within the limits.
