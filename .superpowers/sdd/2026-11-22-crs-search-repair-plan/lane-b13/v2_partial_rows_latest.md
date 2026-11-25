# New-op triage (B13)

Wave: snap-v2-202611240348 (PARTIAL rows, synced ~04:40Z), code 5c43220a (C4 fix, C7, D14, A10, B12; HAS the rollback_detail crash bug). NO traces.

**ROWS-ONLY TRIAGE for every search row below without a trace. Unavailable from rows, and why:**
1. per-op better/worse: rows keep `deltas` for only the FIRST 8 evaluated candidates of the whole search (the rest: `applied:unscored(past deltas cap)`), and each delta is a CHEAP-tier child against its parent state's score, whose tier the row does not record (FULL for the start and every confirmed state). So rows compare ONLY the tier-invariant keys - validity (layer 1) and deviation (layer 3): `applied:made_INVALID` / `made_VALID` / `stays_valid|stays_INVALID:dev_less|dev_same|dev_more`. Panel passes and min SICN are NOT compared across tiers.
2. whether an op is on the best/delivered path: the row has no path; the trace's `stop.best_path` is gone.
3. conformalise mode (fragment vs cut_by) and cut depth: the row's op table is keyed by op name only.
4. never-proposed with vs without a target site: the row's `open_sites` is at the best state only, and `not_proposed` is keyed by reason, not op; so `never_proposed` here also covers 'declined in advance by the sweep'.
5. defeature source (site/recognised) and features per call: candidate params are not in the row.
6. (not available from traces either) a full-panel verdict per candidate: only confirmed states get one.

**CLEAN CONTROL** (every clean-split file, every arm; a regression = input clean, output conclusive and not clean): input_only 40/40 clean; search_ai 40/40 clean; search_det 40/40 clean; staged 40/40 clean. Regressions: 0.

**CRASH ATTRIBUTION IS MEASURED ONLY FOR FINISHED SHARDS:** 2 of 28 shards have EXCLUDED.json on disk. run_arm holds exclusions IN MEMORY until a shard ends, so for the other 26 the harness-crash count is UNKNOWN, not 0; if the wave is stopped at its deadline those reasons are LOST ('excluded before the stop, reason not on disk'), and only the row-gap signature below can point at them.

**EXCLUDED (no row):** none. Partial shards (no EXCLUDED yet): files passed over with arms MISSING 0 (crash signature, input_only alone: 0); files still running 28. `harness_crash:*` pairs died in the HARNESS (e.g. the 5c43220a rollback_detail TypeError): not a repair failure, reported apart from file_wall and deadline. `deadline` pairs were never run before the wave was stopped: UNMEASURED, not failures, and counted nowhere else in this file; `file_wall` is the file's own wall clock.

Rows read: 236. Failing rows (conclusive, not clean): 62. FILES still failing in every repair arm: 22 (kind read off the last CONCLUSIVE arm of search_ai, search_det, staged; inconclusive arms named).

| still-failing kind | files |
|---|---|
| invalid: body_overlap | 5 |
| panel: gmsh_default/low_quality  [read off staged; inconclusive: search_det] | 1 |
| invalid: open_shell, self_intersection | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/periodic_surface | 1 |
| invalid: body_overlap, open_shell | 1 |
| invalid: body_overlap, open_shell, self_intersection  [read off staged; inconclusive: search_ai, search_det] | 1 |
| invalid: open_shell  [read off staged; inconclusive: search_det] | 1 |
| invalid: self_intersection | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/low_quality  [read off staged; inconclusive: search_det] | 1 |
| panel: gmsh_default/edge_through_face, gmsh_prod/other | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/other  [read off staged; inconclusive: search_det] | 1 |
| invalid: self_intersection  [read off staged; inconclusive: search_ai, search_det] | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality | 1 |
| invalid: body_overlap  [read off staged; inconclusive: search_ai, search_det] | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/low_quality | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/other  [read off staged; inconclusive: search_ai, search_det] | 1 |
| panel: gmsh_default/overlapping_facets | 1 |
| invalid: body_overlap, self_intersection | 1 |

Rows per split/arm: snap-v2-clean-202611240348/input_only 40, snap-v2-clean-202611240348/search_ai 40, snap-v2-clean-202611240348/search_det 40, snap-v2-clean-202611240348/staged 40, snap-v2-dev-202611240348/input_only 33, snap-v2-dev-202611240348/search_ai 7, snap-v2-dev-202611240348/search_det 13, snap-v2-dev-202611240348/staged 23
Inconclusive per split/arm: snap-v2-dev-202611240348/search_ai 4, snap-v2-dev-202611240348/search_det 8

Failing search rows triaged from a TRACE: - (the rest from the row's op table: no mode split, deltas capped at 8, no best-path or full-confirm).
Trace extras (defeature source / features per call / holes / blends; proposals per op/source): 

## Failing dev files: did ANY arm improve panel passes or validity short of clean? 3 of 22 yes

| file | any improved | per arm (valid in->out, panel in->out) |
|---|---|---|
| 0051_00518960_bb93f0d63e97fc3782a0c6dd_step_051.step | YES | search_ai: IMPROVED valid False->True panel 1->1/4; search_det: same valid False->False panel 1->1/4; staged: same valid False->False panel 1->1/4 |
| 0056_00561477_ca18416aea564660ee2eea7a_step_006.step | YES | staged: IMPROVED valid True->True panel 2->3/4 |
| 0057_00574915_30a8ce52329868badc4fd335_step_014.step | YES | search_det: IMPROVED valid False->True panel 2->2/4; staged: same valid False->False panel 2->2/4 |
| 00542755_475d3d5812881065217cd6c1_step_002.step | no | search_det: inconclusive; staged: same valid True->True panel 3->3/4 |
| 0051_00510314_5899e652a878da0f8226347a_step_000.step | no | staged: same valid False->False panel 1->1/4 |
| 0051_00510565_589a00b4d2bc860f82f79a88_step_002.step | no | search_ai: inconclusive; search_det: inconclusive; staged: same valid False->False panel 1->1/4 |
| 0051_00510567_589a00b4d2bc860f82f79a88_step_004.step | no | staged: same valid False->False panel 1->1/4 |
| 0051_00512819_0d20478fa07db557be91ad16_step_000.step | no | search_det: inconclusive; staged: same valid False->False panel 2->2/4 |
| 0051_00513828_86650477f66607316f890e2b_step_000.step | no | staged: same valid False->False panel 0->0/4 |
| 0051_00514513_589b5807a417220f72384061_step_000.step | no | search_ai: same valid True->True panel 2->2/4; search_det: same valid True->True panel 2->2/4; staged: same valid True->True panel 2->2/4 |
| 0051_00515810_589bdec38006660f7f91d8e2_step_014.step | no | staged: same valid False->False panel 0->0/4 |
| 0052_00520702_589e0c6497e5700f6ce179d1_step_000.step | no | staged: same valid False->False panel 1->1/4 |
| 0052_00521846_589eac5a2ec4e50f8780d04b_step_006.step | no | staged: same valid False->False panel 0->0/4 |
| 0052_00523933_58a02828f0a1c50ff6ebd077_step_012.step | no | staged: same valid False->False panel 1->1/4 |
| 0052_00524943_fa9a21c8b070173bfc87844b_step_002.step | no | search_det: inconclusive; staged: same valid True->True panel 1->1/4 |
| 0053_00535069_bb0bc8e7914fdea894ad2ec6_step_012.step | no | search_det: inconclusive; staged: same valid True->True panel 1->1/4 |
| 0053_00539073_58a66b7522115e0f6fe163e3_step_000.step | no | search_ai: inconclusive; search_det: inconclusive; staged: same valid True->True panel 1->1/4 |
| 0054_00544196_58a93fda716d73109155df3d_step_090.step | no | search_ai: inconclusive; search_det: inconclusive; staged: same valid False->False panel 2->2/4 |
| 0054_00547086_58aaf7c222115e0f6fe69154_step_014.step | no | staged: same valid False->False panel 0->0/4 |
| 0055_00558985_58af4fd6f1160b0f8d7beca0_step_001.step | no | staged: same valid False->False panel 0->0/4 |
| 0056_00562266_a20a56b200869b8723d9c310_step_018.step | no | search_ai: inconclusive; search_det: inconclusive; staged: same valid False->False panel 0->0/4 |
| 0056_00567964_2dc8697785b603ebd064db60_step_000.step | no | search_det: same valid True->True panel 1->1/4; staged: same valid True->True panel 1->1/4 |

**C4 check (body-op declines once recorded as duplicates):** conformalise `duplicate` 11, declined/failed/invalid 12 on failing search rows. On the tip (C4 fixed) duplicates here are a FINDING, not the old bug.

## By individual failure (a row counts under each of its codes/members)

| failure | files | rows by arm | open sites at best | defeature | fuse | trim_unbounded | conformalise |
|---|---|---|---|---|---|---|---|
| valid:body_overlap | 14 | input_only 14, staged 11, search_det 1 | self_overlap 1 | proposed in 1 rows; applied:stays_INVALID:dev_more 4, applied:unscored(past deltas cap) 3 | proposed in 1 rows; skipped_budget 3 | never proposed | proposed in 1 rows; skipped_budget 5, declined:unresolved_overlap 2, declined:no_shared_interface 1 |
| valid:self_intersection | 8 | input_only 8, staged 6 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_alt/low_quality | 8 | search_ai 1, input_only 6, staged 4, search_det 2 | self_overlap 1, overlap 1, proximity 1 | proposed in 3 rows; applied:stays_INVALID:dev_more 7, applied:unscored(past deltas cap) 2, declined:no_change 1, skipped_budget 1, applied:made_INVALID 1 | proposed in 3 rows; applied:unscored(past deltas cap) 3, declined:invalid_result 1, declined:nothing_to_fuse 1, skipped_budget 1, applied:made_VALID 1 | never proposed | proposed in 3 rows; declined:unresolved_overlap 7, applied:unscored(past deltas cap) 3, declined:no_shared_interface 2, applied:stays_INVALID:dev_more 2, applied:stays_INVALID:dev_same 2, +1 more |
| panel:gmsh_prod/other | 8 | input_only 8, staged 3, search_det 1, search_ai 1 | intersection 2, invalid 2 | proposed in 2 rows; declined:no_change 3, applied:stays_valid:dev_more 2 | proposed in 1 rows; applied:stays_valid:dev_more 1 [not proposed: never_proposed 1] | never proposed | proposed in 2 rows; duplicate 11 |
| panel:gmsh_default/overlapping_facets | 8 | search_ai 1, input_only 6, staged 3 | self_overlap 1 | proposed in 1 rows; applied:stays_INVALID:dev_more 4, applied:unscored(past deltas cap) 2 | proposed in 1 rows; declined:invalid_result 1, applied:unscored(past deltas cap) 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 1, applied:unscored(past deltas cap) 1 |
| valid:open_shell | 5 | input_only 5, staged 4 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/edge_through_face | 4 | input_only 4, staged 2, search_det 1, search_ai 1 | intersection 2, invalid 2 | proposed in 2 rows; declined:no_change 3, applied:stays_valid:dev_more 2 | proposed in 1 rows; applied:stays_valid:dev_more 1 [not proposed: never_proposed 1] | never proposed | proposed in 2 rows; duplicate 11 |
| panel:gmsh_prod/low_quality | 3 | input_only 3, staged 2, search_det 1 | proximity 1 | proposed in 1 rows; declined:no_change 1, skipped_budget 1, applied:made_INVALID 1 | proposed in 1 rows; applied:made_VALID 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 2, applied:made_VALID 2 |
| panel:gmsh_default/low_quality | 4 | input_only 1, staged 2, search_det 2 | overlap 1, proximity 1 | proposed in 2 rows; applied:stays_INVALID:dev_more 3, declined:no_change 1, skipped_budget 1, applied:made_INVALID 1 | proposed in 2 rows; applied:unscored(past deltas cap) 2, declined:nothing_to_fuse 1, skipped_budget 1, applied:made_VALID 1 | never proposed | proposed in 2 rows; declined:unresolved_overlap 6, declined:no_shared_interface 2, applied:stays_INVALID:dev_more 2, applied:stays_INVALID:dev_same 2, applied:unscored(past deltas cap) 2, +1 more |
| panel:gmsh_alt/timeout | 4 | input_only 4 | - | never proposed | never proposed | never proposed | never proposed |
| valid:brep_invalid | 3 | input_only 3 | - | never proposed | never proposed | never proposed | never proposed |
| panel:netgen_occ/netgen_timeout | 3 | input_only 3 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/timeout | 2 | input_only 2 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_prod/periodic_surface | 1 | search_ai 1 | self_overlap 1 | proposed in 1 rows; applied:stays_INVALID:dev_more 4, applied:unscored(past deltas cap) 2 | proposed in 1 rows; declined:invalid_result 1, applied:unscored(past deltas cap) 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 1, applied:unscored(past deltas cap) 1 |
| panel:netgen_occ/netgen_meshing_failed | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_prod/gmsh_crash | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |

## By full failure kind

| failure | files | rows by arm | open sites at best | defeature | fuse | trim_unbounded | conformalise |
|---|---|---|---|---|---|---|---|
| invalid: body_overlap | 8 | input_only 8, staged 8, search_det 1 | self_overlap 1 | proposed in 1 rows; applied:stays_INVALID:dev_more 4, applied:unscored(past deltas cap) 3 | proposed in 1 rows; skipped_budget 3 | never proposed | proposed in 1 rows; skipped_budget 5, declined:unresolved_overlap 2, declined:no_shared_interface 1 |
| invalid: self_intersection | 3 | input_only 3, staged 3 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_default/edge_through_face, gmsh_prod/other | 2 | input_only 2, staged 1, search_det 1, search_ai 1 | intersection 2, invalid 2 | proposed in 2 rows; declined:no_change 3, applied:stays_valid:dev_more 2 | proposed in 1 rows; applied:stays_valid:dev_more 1 [not proposed: never_proposed 1] | never proposed | proposed in 2 rows; duplicate 11 |
| invalid: body_overlap, open_shell | 2 | input_only 2, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/low_quality | 2 | input_only 2, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_default/low_quality | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: open_shell, self_intersection | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, open_shell, self_intersection | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: open_shell | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/low_quality | 2 | staged 1, search_det 1 | proximity 1 | proposed in 1 rows; declined:no_change 1, skipped_budget 1, applied:made_INVALID 1 | proposed in 1 rows; applied:made_VALID 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 2, applied:made_VALID 2 |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/other | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/other | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, self_intersection | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, brep_invalid, self_intersection | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: brep_invalid, self_intersection | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/periodic_surface | 1 | search_ai 1 | self_overlap 1 | proposed in 1 rows; applied:stays_INVALID:dev_more 4, applied:unscored(past deltas cap) 2 | proposed in 1 rows; declined:invalid_result 1, applied:unscored(past deltas cap) 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 1, applied:unscored(past deltas cap) 1 |
| panel: gmsh_alt/timeout, gmsh_default/timeout, gmsh_prod/other | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/timeout, gmsh_default/overlapping_facets, gmsh_prod/other | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/overlapping_facets, gmsh_prod/low_quality, netgen_occ/netgen_timeout | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/timeout, gmsh_default/overlapping_facets, gmsh_prod/other, netgen_occ/netgen_timeout | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/other, netgen_occ/netgen_meshing_failed | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/timeout, gmsh_default/timeout, gmsh_prod/gmsh_crash, netgen_occ/netgen_timeout | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality | 1 | search_det 1 | overlap 1 | proposed in 1 rows; applied:stays_INVALID:dev_more 3 | proposed in 1 rows; applied:unscored(past deltas cap) 2, declined:nothing_to_fuse 1, skipped_budget 1 | never proposed | proposed in 1 rows; declined:unresolved_overlap 4, declined:no_shared_interface 2, applied:stays_INVALID:dev_more 2, applied:stays_INVALID:dev_same 2, applied:unscored(past deltas cap) 2 |
| invalid: body_overlap, brep_invalid | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_default/overlapping_facets | 1 | staged 1 | - | never proposed | never proposed | never proposed | never proposed |
