# New-op triage (B13)

Wave: snap-v1r-202611240237 (PARTIAL rows), code a9dae3bb: HAS the C4 fix ad716ef4 and B7/B8b; does NOT have B12 (caps), C7 (fuse/cut any depth/trim_unbounded) or D14 (defeature), so those columns cannot bite here.

**ROWS-ONLY TRIAGE for every search row below without a trace. Unavailable from rows, and why:**
1. per-op better/equal/worse: rows keep `deltas` for only the FIRST 8 evaluated candidates of the whole search; the rest show as `applied->unscored`.
2. whether an op is on the best/delivered path: the row has no path; the trace's `stop.best_path` is gone.
3. conformalise mode (fragment vs cut_by) and cut depth: the row's op table is keyed by op name only.
4. never-proposed with vs without a target site: the row's `open_sites` is at the best state only, and `not_proposed` is keyed by reason, not op; so `never_proposed` here also covers 'declined in advance by the sweep'.
5. defeature source (site/recognised) and features per call: candidate params are not in the row.
6. (not available from traces either) a full-panel verdict per candidate: only confirmed states get one.

Rows read: 73. Failing rows (conclusive, not clean): 31. FILES still failing in every repair arm: 9 (kind read off the last CONCLUSIVE arm of search_ai, search_det, staged; inconclusive arms named).

| still-failing kind | files |
|---|---|
| invalid: body_overlap | 3 |
| invalid: body_overlap, brep_invalid, open_shell | 1 |
| panel: gmsh_alt/no_volume, gmsh_prod/other, netgen_occ/netgen_meshing_failed | 1 |
| invalid: body_overlap  [read off staged; inconclusive: search_det] | 1 |
| panel: gmsh_alt/no_volume, gmsh_default/other, gmsh_prod/other  [read off staged; inconclusive: search_ai, search_det] | 1 |
| panel: gmsh_alt/no_volume, gmsh_default/overlapping_facets, gmsh_prod/other, netgen_occ/netgen_crash  [read off search_det; inconclusive: search_ai] | 1 |
| invalid: body_overlap, self_intersection  [read off staged; inconclusive: search_ai, search_det] | 1 |

Rows per split/arm: snap-v1r-clean-202611240237/input_only 9, snap-v1r-clean-202611240237/search_ai 9, snap-v1r-clean-202611240237/search_det 9, snap-v1r-clean-202611240237/staged 9, snap-v1r-dev-202611240237/input_only 13, snap-v1r-dev-202611240237/search_ai 7, snap-v1r-dev-202611240237/search_det 8, snap-v1r-dev-202611240237/staged 9
Inconclusive per split/arm: snap-v1r-dev-202611240237/search_ai 3, snap-v1r-dev-202611240237/search_det 3

Failing search rows triaged from a TRACE: - (the rest from the row's op table: no mode split, deltas capped at 8, no best-path or full-confirm).
Trace extras (defeature source / features per call / holes / blends; proposals per op/source): 

**C4 check:** conformalise `duplicate` 0, declined/failed/invalid 49 on failing search rows.

## By individual failure (a row counts under each of its codes/members)

| failure | files | rows by arm | open sites at best | defeature | fuse | trim_unbounded | conformalise |
|---|---|---|---|---|---|---|---|
| valid:body_overlap | 7 | input_only 7, staged 6, search_det 3, search_ai 3 | invalid 2, overlap 6, intersection 4 | never proposed | never proposed | never proposed | proposed in 6 rows; declined:unresolved_overlap 23, declined:exceeds_fidelity 14, declined:no_shared_interface 12 |
| panel:gmsh_prod/other | 4 | input_only 4, staged 3, search_det 2, search_ai 1 | invalid 3, open 2, self_overlap 1 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_alt/no_volume | 3 | input_only 3, staged 3, search_det 2, search_ai 1 | invalid 3, open 2, self_overlap 1 | never proposed | never proposed | never proposed | never proposed |
| panel:netgen_occ/netgen_meshing_failed | 2 | input_only 2, staged 1, search_det 1, search_ai 1 | invalid 2, open 2 | never proposed | never proposed | never proposed | never proposed |
| valid:open_shell | 3 | input_only 3, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| valid:brep_invalid | 2 | input_only 2, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/edge_through_face | 3 | input_only 3 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/other | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/overlapping_facets | 1 | staged 1, search_det 1 | invalid 1, self_overlap 1 | never proposed | never proposed | never proposed | never proposed |
| panel:netgen_occ/netgen_crash | 1 | staged 1, search_det 1 | invalid 1, self_overlap 1 | never proposed | never proposed | never proposed | never proposed |
| valid:self_intersection | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_alt/low_quality | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |

## By full failure kind

| failure | files | rows by arm | open sites at best | defeature | fuse | trim_unbounded | conformalise |
|---|---|---|---|---|---|---|---|
| invalid: body_overlap | 5 | input_only 5, staged 4, search_det 3, search_ai 3 | invalid 2, overlap 6, intersection 4 | never proposed | never proposed | never proposed | proposed in 6 rows; declined:unresolved_overlap 23, declined:exceeds_fidelity 14, declined:no_shared_interface 12 |
| panel: gmsh_alt/no_volume, gmsh_prod/other, netgen_occ/netgen_meshing_failed | 1 | staged 1, search_det 1, search_ai 1 | invalid 2, open 2 | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, brep_invalid, open_shell | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/no_volume, gmsh_default/edge_through_face, gmsh_prod/other, netgen_occ/netgen_meshing_failed | 2 | input_only 2 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/no_volume, gmsh_default/other, gmsh_prod/other | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/no_volume, gmsh_default/overlapping_facets, gmsh_prod/other, netgen_occ/netgen_crash | 1 | staged 1, search_det 1 | invalid 1, self_overlap 1 | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, self_intersection | 1 | input_only 1, staged 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: open_shell | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| invalid: brep_invalid, open_shell | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/other | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
