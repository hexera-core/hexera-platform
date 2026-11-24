# D24 BLOCKER FAMILIES (coarse): 101 dev files not clean after pass 1 + D24 (depth 3, 4 h)

| rank | blocker family (best state reached) | files | killed / exhausted | deepest | op families tried most | example |
|---|---|---|---|---|---|---|
| 1 | mesh (valid): gmsh_default overlapping_facets (+2 more) | 18 | 8 / 10 | d3 | conformalise, split_angle, run_stage, rebuild_face | f9501251fb |
| 2 | L1 invalid: body_overlap (+other L1) | 14 | 8 / 6 | d3 | conformalise, fuse, run_stage, split_angle | 1e6f8d8e11 |
| 3 | L1 invalid: body_overlap | 8 | 5 / 3 | d3 | conformalise, fuse, run_stage, split_angle | 01413e3ff0 |
| 4 | mesh (valid): gmsh_default edge_through_face (+2 more) | 7 | 1 / 6 | d3 | conformalise, run_stage, split_angle, fuse | 80c009cb74 |
| 5 | L1 invalid: self_intersection | 7 | 4 / 3 | d3 | run_stage, split_angle, unify_same_domain, orient_and_close | 7b13ec12fe |
| 6 | mesh (valid): gmsh_default low_quality (+2 more) | 6 | 3 / 3 | d3 | run_stage, split_angle, unify_same_domain, remove_small | 722c122c9b |
| 7 | mesh (valid): gmsh_default overlapping_facets (+1 more) | 6 | 1 / 5 | d3 | conformalise, split_angle, run_stage, defeature | f56dd3b77d |
| 8 | mesh (valid): gmsh_default overlapping_facets (+3 more) | 5 | 2 / 3 | d3 | run_stage, split_angle, remove_small, unify_same_domain | 9d0d49a626 |
| 9 | mesh (valid): gmsh_default edge_through_face (+1 more) | 5 | 1 / 4 | d3 | conformalise, run_stage, fuse, split_angle | 4f0996fa76 |
| 10 | mesh (valid): gmsh_default edge_through_face (+3 more) | 3 | 2 / 1 | d3 | split_angle, run_stage, rebuild_face, unify_same_domain | 557c3c0563 |
| 11 | mesh (valid): gmsh_default low_quality (+1 more) | 3 | 0 / 3 | d3 | split_angle, unify_same_domain, run_stage, defeature | 3eb61cabb3 |
| 12 | mesh (valid): gmsh_default overlapping_facets | 3 | 1 / 2 | d3 | run_stage, defeature, split_angle, remove_small | a2807f9a2f |
| 13 | L1 invalid: self_intersection (+other L1) | 2 | 2 / 0 | d1 | split_angle, remove_small, fill_loop, split_continuity | 782680a429 |
| 14 | mesh (valid): gmsh_default low_quality (+3 more) | 2 | 0 / 2 | d3 | split_angle, remove_small, run_stage, rebuild_face | f1eef4cd07 |
| 15 | mesh (valid): gmsh_default periodic_surface (+3 more) | 2 | 2 / 0 | d3 | conformalise, split_angle, fuse, run_stage | 96c7bdaf49 |
| 16 | L1 invalid: unbounded_geometry | 1 | 0 / 1 | d2 | run_stage, trim_unbounded, orient_and_close | 97314ccbe4 |
| 17 | mesh (valid): gmsh_default timeout (+3 more) | 1 | 1 / 0 | d0 |  | 0522f19877 |
| 18 | mesh (valid): gmsh_default timeout (+2 more) | 1 | 1 / 0 | d1 | orient_and_close, unify_same_domain, run_stage | 36399c6239 |
| 19 | mesh (valid): gmsh_default no_volume (+3 more) | 1 | 0 / 1 | d2 | run_stage, defeature, orient_and_close, split_by_proximity | 5abbaf0b55 |
| 20 | mesh (valid): gmsh_default low_quality | 1 | 0 / 1 | d3 | defeature, run_stage, remove_small, conformalise | 7917f3122c |
| 21 | mesh (valid): gmsh_prod low_quality | 1 | 0 / 1 | d3 | split_angle, unify_same_domain, run_stage, rebuild_face | a0bc551656 |
| 22 | L1 invalid: brep_invalid | 1 | 0 / 1 | d1 | run_stage, orient_and_close | 532be4434c |
| 23 | mesh (valid): gmsh_prod other | 1 | 0 / 1 | d3 | split_angle, run_stage, split_continuity, defeature | 75e9d59b46 |
| 24 | mesh (valid): gmsh_default edge_through_face | 1 | 0 / 1 | d3 | run_stage, split_angle, reapproximate, split_by_proximity | b25999e0f8 |
| 25 | mesh (valid): netgen_occ netgen_meshing_failed | 1 | 0 / 1 | d2 | run_stage, split_angle, orient_and_close, split_continuity | 6946c8aa04 |
