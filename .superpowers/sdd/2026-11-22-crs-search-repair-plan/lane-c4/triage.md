# Failure triage: .c4wave

Counts are files (one row per file and arm) unless 'candidates'. Only conclusive, NOT clean outputs are explained; inconclusive and excluded files are counted apart, because nothing about the part made them so.

## Leads (largest first)

| arm | what | key | files | of not clean | candidates |
|---|---|---|---|---|---|
| search_det | stop reason | exhausted | 13 | 13 | - |
| search_det | open site kind | unlocated | 12 | 13 | - |
| search_det | panel member still failing | gmsh_prod: other | 8 | 13 | - |
| search_det | layer-1 hard finding | body_overlap | 7 | 13 | - |
| search_det | op invalid | remove_small: targets[0]: F2 is a face, not an any | 1 | 13 | 3 |
| search_det | op declined | unify_same_domain: no_change: unify_same_domain left the part as it was | 1 | 13 | 1 |

## search_det

files 25: clean 1, NOT clean 13, inconclusive rows 0, excluded 11 (checker_unavailable (6); file_timeout (4); inconclusive (1))
not clean by defect kind: other (6); body_overlap (4); brep_invalid (1); periodic_surface (1); self_intersection (1)

stop reasons: exhausted (13) (ended by: exhausted (13))
open site kinds at stop (files): unlocated (12); overlap (4); self_overlap (1); sliver (1); 18 sites open in all

| op | files | proposed det | proposed ai | evaluated | declined | failed | invalid | duplicate | skipped (budget) | top declined | top failed | top invalid |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| conformalise | 4 | 8 | 0 | 0 | 0 | 0 | 0 | 8 | 0 | - | - | - |
| remove_small | 1 | 3 | 0 | 0 | 0 | 0 | 3 | 0 | 0 | - | - | targets[0]: F2 is a face, not an any (3) |
| unify_same_domain | 1 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | no_change: unify_same_domain left the part as it was (1) | - | - |

members failing per file: {'0': 1, '1': 2, '3': 8, '4': 2}

| panel member | files failing | status | error kinds |
|---|---|---|---|
| gmsh_prod | 12 | fail (12) | other (8); low_quality (2); open_loop (1); periodic_surface (1) |
| gmsh_default | 9 | fail (9) | edge_through_face (4); low_quality (1); no_volume (1); open_loop (1); other (1) |
| gmsh_alt | 7 | fail (7) | no_volume (4); low_quality (2); open_loop (1) |
| netgen_occ | 6 | fail (5); crashed (1) | netgen_meshing_failed (5); netgen_crash (1) |

layer-1 hard findings: body_overlap (7); brep_invalid (1); self_intersection (1)
deviation: faithful failed on 0, over the bound on 0; budget used median 0.00, p90 0.00, max 0.00 (1.0 = the bound); reasons -

## Per file (not-clean search rows)

| file | arm | stop | panel in->out | open sites | ops | deltas | failing members |
|---|---|---|---|---|---|---|---|
| /00613121_58c67ddf032617103d59c101_step_003.step | search_det | exhausted | 4 -> 4 | overlap (1) | conformalise: det 2, duplicate 2 | - | - |
| les/AP203 geometry only/nist_ctc_01_asme1_rd.stp | search_det | exhausted | 3 -> 3 | unlocated (1) | - | - | gmsh_prod: low_quality |
| Files/AP203 with PMI/nist_ctc_01_asme1_ap203.stp | search_det | exhausted | 3 -> 3 | unlocated (1) | - | - | gmsh_prod: low_quality |
| _00519158_daf80b2021249a203e938429_step_009.step | search_det | exhausted | 0 -> 1 | unlocated (1) | - | - | gmsh_prod: other, gmsh_alt: no_volume, netgen_occ: netgen_meshing_failed |
| _00521884_f7d1e4b9658237600ec82613_step_012.step | search_det | exhausted | 1 -> 1 | overlap (1); unlocated (1) | conformalise: det 2, duplicate 2 | - | gmsh_prod: other, gmsh_default: edge_through_face, netgen_occ: netgen_meshing_failed |
| _00526988_58a1d33fd3d9a2108b54be6d_step_000.step | search_det | exhausted | 1 -> 1 | unlocated (1) | - | - | gmsh_prod: other, gmsh_default: other, gmsh_alt: no_volume |
| _00541957_51229e00ebc25e88ce807b9e_step_000.step | search_det | exhausted | 1 -> 1 | overlap (1); unlocated (1) | conformalise: det 2, duplicate 2 | - | gmsh_prod: other, gmsh_default: edge_through_face, gmsh_alt: low_quality |
| _00547929_58ab27a87442550f6daa230f_step_014.step | search_det | exhausted | 1 -> 1 | unlocated (1) | - | - | gmsh_prod: other, gmsh_default: low_quality, gmsh_alt: low_quality |
| _00550171_58ac32b095b047102fd8ce8c_step_013.step | search_det | exhausted | 0 -> 0 | unlocated (1) | - | - | gmsh_prod: other, gmsh_default: overlapping_facets, gmsh_alt: no_volume, netgen_occ: netgen_crash |
| _00570551_59995fb3cd27b48fe0957ae2_step_007.step | search_det | exhausted | 1 -> 1 | unlocated (1) | - | - | gmsh_prod: periodic_surface, gmsh_default: no_volume, gmsh_alt: no_volume |
| _00608014_3fedd89a1d6b0c6f1c34e1b7_step_001.step | search_det | exhausted | 0 -> 0 | sliver (1); unlocated (1) | remove_small: det 3, invalid 3 (targets[0]: F2 is a face, not an any); unify_same_domain: det 1, declined 1 (no_change: unify_same_domain left the part as it was) | - | gmsh_prod: open_loop, gmsh_default: open_loop, gmsh_alt: open_loop, netgen_occ: netgen_meshing_failed |
| occt/trj3_s1-db-214.stp | search_det | exhausted | 1 -> 1 | self_overlap (1); unlocated (1) | - | - | gmsh_prod: other, gmsh_default: edge_through_face, netgen_occ: netgen_meshing_failed |
| occt/trj4_s1-tu-214.stp | search_det | exhausted | 1 -> 1 | overlap (1); unlocated (1) | conformalise: det 2, duplicate 2 | - | gmsh_prod: other, gmsh_default: edge_through_face, netgen_occ: netgen_meshing_failed |
