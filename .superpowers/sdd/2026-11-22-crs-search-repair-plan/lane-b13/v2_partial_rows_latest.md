# New-op triage (B13)

Wave: snap-v2-202611240348 (PARTIAL rows, synced ~04:20Z), code 5c43220a: HAS C4 fix ad716ef4, C7, D14, A10, B12. NO traces in this wave.

**ROWS-ONLY TRIAGE for every search row below without a trace. Unavailable from rows, and why:**
1. per-op better/equal/worse: rows keep `deltas` for only the FIRST 8 evaluated candidates of the whole search; the rest show as `applied->unscored`.
2. whether an op is on the best/delivered path: the row has no path; the trace's `stop.best_path` is gone.
3. conformalise mode (fragment vs cut_by) and cut depth: the row's op table is keyed by op name only.
4. never-proposed with vs without a target site: the row's `open_sites` is at the best state only, and `not_proposed` is keyed by reason, not op; so `never_proposed` here also covers 'declined in advance by the sweep'.
5. defeature source (site/recognised) and features per call: candidate params are not in the row.
6. (not available from traces either) a full-panel verdict per candidate: only confirmed states get one.

**CLEAN CONTROL** (every clean-split file, every arm; a regression = input clean, output conclusive and not clean): input_only 40/40 clean; search_ai 40/40 clean; search_det 40/40 clean; staged 40/40 clean. Regressions: 0.

**EXCLUDED (no row):** none. `deadline` pairs were never run before the wave was stopped: UNMEASURED, not failures, and counted nowhere else in this file; `file_wall` is the file's own wall clock.

Rows read: 207. Failing rows (conclusive, not clean): 43. FILES still failing in every repair arm: 15 (kind read off the last CONCLUSIVE arm of search_ai, search_det, staged; inconclusive arms named).

| still-failing kind | files |
|---|---|
| invalid: body_overlap | 5 |
| panel: gmsh_default/low_quality | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/periodic_surface | 1 |
| invalid: body_overlap, open_shell, self_intersection  [read off staged; inconclusive: search_det] | 1 |
| invalid: open_shell | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/low_quality | 1 |
| panel: gmsh_default/edge_through_face, gmsh_prod/other | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/other | 1 |
| invalid: self_intersection  [read off staged; inconclusive: search_det] | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/other | 1 |
| invalid: body_overlap, self_intersection | 1 |

Rows per split/arm: snap-v2-clean-202611240348/input_only 40, snap-v2-clean-202611240348/search_ai 40, snap-v2-clean-202611240348/search_det 40, snap-v2-clean-202611240348/staged 40, snap-v2-dev-202611240348/input_only 23, snap-v2-dev-202611240348/search_ai 3, snap-v2-dev-202611240348/search_det 5, snap-v2-dev-202611240348/staged 16
Inconclusive per split/arm: snap-v2-dev-202611240348/search_det 2

Failing search rows triaged from a TRACE: - (the rest from the row's op table: no mode split, deltas capped at 8, no best-path or full-confirm).
Trace extras (defeature source / features per call / holes / blends; proposals per op/source): 

## Failing dev files: did ANY arm improve panel passes or validity short of clean? 1 of 15 yes

| file | any improved | per arm (valid in->out, panel in->out) |
|---|---|---|
| 0051_00518960_bb93f0d63e97fc3782a0c6dd_step_051.step | YES | search_ai: IMPROVED valid False->True panel 1->1/4; search_det: same valid False->False panel 1->1/4; staged: same valid False->False panel 1->1/4 |
| 00542755_475d3d5812881065217cd6c1_step_002.step | no | staged: same valid True->True panel 3->3/4 |
| 0051_00510314_5899e652a878da0f8226347a_step_000.step | no | staged: same valid False->False panel 1->1/4 |
| 0051_00510565_589a00b4d2bc860f82f79a88_step_002.step | no | search_det: inconclusive; staged: same valid False->False panel 1->1/4 |
| 0051_00510567_589a00b4d2bc860f82f79a88_step_004.step | no | staged: same valid False->False panel 1->1/4 |
| 0051_00512819_0d20478fa07db557be91ad16_step_000.step | no | staged: same valid False->False panel 2->2/4 |
| 0051_00514513_589b5807a417220f72384061_step_000.step | no | search_ai: same valid True->True panel 2->2/4; search_det: same valid True->True panel 2->2/4; staged: same valid True->True panel 2->2/4 |
| 0051_00515810_589bdec38006660f7f91d8e2_step_014.step | no | staged: same valid False->False panel 0->0/4 |
| 0052_00523933_58a02828f0a1c50ff6ebd077_step_012.step | no | staged: same valid False->False panel 1->1/4 |
| 0052_00524943_fa9a21c8b070173bfc87844b_step_002.step | no | staged: same valid True->True panel 1->1/4 |
| 0053_00535069_bb0bc8e7914fdea894ad2ec6_step_012.step | no | staged: same valid True->True panel 1->1/4 |
| 0053_00539073_58a66b7522115e0f6fe163e3_step_000.step | no | staged: same valid True->True panel 1->1/4 |
| 0054_00544196_58a93fda716d73109155df3d_step_090.step | no | staged: same valid False->False panel 2->2/4 |
| 0054_00547086_58aaf7c222115e0f6fe69154_step_014.step | no | staged: same valid False->False panel 0->0/4 |
| 0056_00562266_a20a56b200869b8723d9c310_step_018.step | no | search_det: inconclusive; staged: same valid False->False panel 0->0/4 |

**C4 check (body-op declines once recorded as duplicates):** conformalise `duplicate` 11, declined/failed/invalid 4 on failing search rows. On the tip (C4 fixed) duplicates here are a FINDING, not the old bug.

## By individual failure (a row counts under each of its codes/members)

| failure | files | rows by arm | open sites at best | defeature | fuse | trim_unbounded | conformalise |
|---|---|---|---|---|---|---|---|
| valid:body_overlap | 11 | input_only 11, staged 8, search_det 1 | self_overlap 1 | proposed in 1 rows; applied->worse 4, applied->unscored 3 | proposed in 1 rows; skipped_budget 3 | never proposed | proposed in 1 rows; skipped_budget 5, declined:unresolved_overlap 2, declined:no_shared_interface 1 |
| valid:self_intersection | 6 | input_only 6, staged 4 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_prod/other | 5 | input_only 5, staged 3, search_det 1, search_ai 1 | intersection 2, invalid 2 | proposed in 2 rows; declined:no_change 3, applied->worse 2 | proposed in 1 rows; applied->worse 1 [not proposed: never_proposed 1] | never proposed | proposed in 2 rows; duplicate 11 |
| panel:gmsh_alt/low_quality | 6 | search_ai 1, input_only 5, staged 3 | self_overlap 1 | proposed in 1 rows; applied->worse 4, applied->unscored 2 | proposed in 1 rows; declined:invalid_result 1, applied->unscored 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 1, applied->unscored 1 |
| panel:gmsh_default/edge_through_face | 3 | input_only 3, staged 2, search_det 1, search_ai 1 | intersection 2, invalid 2 | proposed in 2 rows; declined:no_change 3, applied->worse 2 | proposed in 1 rows; applied->worse 1 [not proposed: never_proposed 1] | never proposed | proposed in 2 rows; duplicate 11 |
| panel:gmsh_default/overlapping_facets | 5 | search_ai 1, input_only 4, staged 1 | self_overlap 1 | proposed in 1 rows; applied->worse 4, applied->unscored 2 | proposed in 1 rows; declined:invalid_result 1, applied->unscored 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 1, applied->unscored 1 |
| valid:open_shell | 3 | input_only 3, staged 2 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/low_quality | 2 | input_only 1, staged 2 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_prod/low_quality | 2 | input_only 2, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| valid:brep_invalid | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_prod/periodic_surface | 1 | search_ai 1 | self_overlap 1 | proposed in 1 rows; applied->worse 4, applied->unscored 2 | proposed in 1 rows; declined:invalid_result 1, applied->unscored 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 1, applied->unscored 1 |
| panel:gmsh_alt/timeout | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel:netgen_occ/netgen_timeout | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel:netgen_occ/netgen_meshing_failed | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |

## By full failure kind

| failure | files | rows by arm | open sites at best | defeature | fuse | trim_unbounded | conformalise |
|---|---|---|---|---|---|---|---|
| invalid: body_overlap | 7 | input_only 7, staged 6, search_det 1 | self_overlap 1 | proposed in 1 rows; applied->worse 4, applied->unscored 3 | proposed in 1 rows; skipped_budget 3 | never proposed | proposed in 1 rows; skipped_budget 5, declined:unresolved_overlap 2, declined:no_shared_interface 1 |
| invalid: self_intersection | 3 | input_only 3, staged 2 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_default/edge_through_face, gmsh_prod/other | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | intersection 2, invalid 2 | proposed in 2 rows; declined:no_change 3, applied->worse 2 | proposed in 1 rows; applied->worse 1 [not proposed: never_proposed 1] | never proposed | proposed in 2 rows; duplicate 11 |
| panel: gmsh_default/low_quality | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, open_shell, self_intersection | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: open_shell | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/other | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/other | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, self_intersection | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, brep_invalid, self_intersection | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/periodic_surface | 1 | search_ai 1 | self_overlap 1 | proposed in 1 rows; applied->worse 4, applied->unscored 2 | proposed in 1 rows; declined:invalid_result 1, applied->unscored 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 1, applied->unscored 1 |
| invalid: body_overlap, open_shell | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/timeout, gmsh_default/overlapping_facets, gmsh_prod/other | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/low_quality, netgen_occ/netgen_timeout | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/other, netgen_occ/netgen_meshing_failed | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/low_quality | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/low_quality | 1 | staged 1 | - | never proposed | never proposed | never proposed | never proposed |
