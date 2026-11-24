# New-op triage (B13)

Rows read: 146. Failing rows (conclusive, not clean): 108. FILES still failing in every repair arm: 26 (kind read off search_ai, else search_det, else staged).

| still-failing kind | files |
|---|---|
| invalid: body_overlap | 7 |
| panel: gmsh_prod/low_quality | 2 |
| invalid: body_overlap, self_intersection | 2 |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/other | 2 |
| invalid: brep_invalid, open_shell | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/other, netgen_occ/netgen_crash | 1 |
| panel: gmsh_alt/no_volume, gmsh_default/other, gmsh_prod/other | 1 |
| panel: gmsh_alt/no_volume, gmsh_default/edge_through_face, gmsh_prod/other, netgen_occ/netgen_meshing_failed | 1 |
| panel: gmsh_default/overlapping_facets, gmsh_prod/low_quality | 1 |
| panel: gmsh_alt/no_volume, gmsh_prod/other, netgen_occ/netgen_meshing_failed | 1 |
| panel: gmsh_alt/low_quality, gmsh_prod/low_quality, netgen_occ/low_quality | 1 |
| invalid: open_shell | 1 |
| invalid: brep_invalid | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/other | 1 |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/low_quality | 1 |
| panel: gmsh_prod/other | 1 |
| panel: gmsh_alt/no_volume, gmsh_default/no_volume, gmsh_prod/periodic_surface | 1 |

Rows per split/arm: snap-v1-clean/input_only 8, snap-v1-clean/search_ai 8, snap-v1-clean/search_det 8, snap-v1-clean/staged 8, snap-v1-dev/input_only 30, snap-v1-dev/search_ai 28, snap-v1-dev/search_det 28, snap-v1-dev/staged 28
Inconclusive per split/arm: -

## By individual failure (a row counts under each of its codes/members)

| failure | files | rows by arm | open sites at best | defeature | fuse | conformalise | trim_unbounded |
|---|---|---|---|---|---|---|---|
| valid:body_overlap | 10 | input_only 10, staged 9, search_det 9, search_ai 9 | overlap 14, unlocated 16, self_overlap 4 | never proposed | never proposed | proposed in 16 rows; duplicate 37 | never proposed |
| panel:gmsh_prod/other | 10 | input_only 8, staged 9, search_det 8, search_ai 8 | unlocated 16 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_alt/low_quality | 6 | input_only 5, staged 6, search_det 6, search_ai 6 | unlocated 12 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_prod/low_quality | 6 | input_only 5, staged 5, search_det 5, search_ai 5 | unlocated 10 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_alt/no_volume | 5 | input_only 5, staged 4, search_det 4, search_ai 4 | unlocated 8 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/low_quality | 5 | input_only 4, staged 4, search_det 4, search_ai 4 | unlocated 8 | never proposed | never proposed | never proposed | never proposed |
| valid:open_shell | 4 | input_only 4, staged 2, search_det 2, search_ai 2 | open 4, sliver 2, unlocated 4 | never proposed | never proposed | never proposed | never proposed |
| valid:brep_invalid | 3 | input_only 3, staged 2, search_det 2, search_ai 2 | open 2, sliver 4, unlocated 4 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/edge_through_face | 4 | input_only 4, search_ai 2, staged 1, search_det 1 | unlocated 3 | never proposed | never proposed | never proposed | never proposed |
| valid:self_intersection | 2 | input_only 2, staged 2, search_det 2, search_ai 2 | self_overlap 4, unlocated 4 | never proposed | never proposed | proposed in 2 rows; duplicate 2 | never proposed |
| panel:netgen_occ/netgen_crash | 2 | input_only 1, staged 2, search_det 2, search_ai 1 | unlocated 3 | never proposed | never proposed | never proposed | never proposed |
| panel:netgen_occ/netgen_meshing_failed | 2 | input_only 2, search_ai 2, staged 1, search_det 1 | unlocated 3 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/overlapping_facets | 2 | staged 2, search_det 2, input_only 1, search_ai 1 | unlocated 3 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/no_volume | 2 | input_only 2, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_default/other | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel:netgen_occ/low_quality | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel:gmsh_prod/periodic_surface | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |

## By full failure kind

| failure | files | rows by arm | open sites at best | defeature | fuse | conformalise | trim_unbounded |
|---|---|---|---|---|---|---|---|
| invalid: body_overlap | 7 | input_only 7, staged 7, search_det 7, search_ai 7 | overlap 14, unlocated 12 | never proposed | never proposed | proposed in 14 rows; duplicate 35 | never proposed |
| panel: gmsh_prod/low_quality | 2 | input_only 2, staged 2, search_det 2, search_ai 2 | unlocated 4 | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, self_intersection | 2 | input_only 2, staged 2, search_det 2, search_ai 2 | self_overlap 4, unlocated 4 | never proposed | never proposed | proposed in 2 rows; duplicate 2 | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/other | 2 | staged 2, search_det 2, search_ai 2, input_only 1 | unlocated 4 | never proposed | never proposed | never proposed | never proposed |
| invalid: open_shell | 2 | input_only 2, staged 1, search_det 1, search_ai 1 | open 2, unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_prod/other | 2 | input_only 1, staged 2, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| invalid: brep_invalid, open_shell | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | open 2, sliver 2, unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/other, netgen_occ/netgen_crash | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/no_volume, gmsh_default/other, gmsh_prod/other | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_default/overlapping_facets, gmsh_prod/low_quality | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| invalid: brep_invalid | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | sliver 2, unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/other | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/low_quality, gmsh_prod/low_quality | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/no_volume, gmsh_default/no_volume, gmsh_prod/periodic_surface | 1 | input_only 1, staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/no_volume, gmsh_default/edge_through_face, gmsh_prod/other, netgen_occ/netgen_meshing_failed | 2 | input_only 2, search_ai 1 | unlocated 1 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/no_volume, gmsh_prod/other, netgen_occ/netgen_meshing_failed | 1 | staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_prod/low_quality, netgen_occ/low_quality | 1 | staged 1, search_det 1, search_ai 1 | unlocated 2 | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/no_volume, gmsh_default/overlapping_facets, gmsh_prod/other, netgen_occ/netgen_crash | 1 | staged 1, search_det 1 | unlocated 1 | never proposed | never proposed | never proposed | never proposed |
| invalid: body_overlap, brep_invalid, open_shell | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/no_volume, gmsh_default/no_volume, gmsh_prod/other, netgen_occ/low_quality | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_alt/low_quality, gmsh_default/edge_through_face, gmsh_prod/low_quality | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
| panel: gmsh_default/low_quality | 1 | input_only 1 | - | never proposed | never proposed | never proposed | never proposed |
