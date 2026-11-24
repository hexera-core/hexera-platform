# D24 blocker table: 101 dev files still NOT clean (of 150)

| rank | blocker on the best state reached | files | killed | exhausted | example |
|---|---|---|---|---|---|
| 1 | L1 invalid: body_overlap | 8 | 5 | 3 | 01413e3ff0 (3443 cands, d3) |
| 2 | mesh: gmsh_alt:low_quality, gmsh_default:overlapping_facets, gmsh_prod:other | 8 | 4 | 4 | f9501251fb (2289 cands, d3) |
| 3 | L1 invalid: self_intersection | 7 | 4 | 3 | 7b13ec12fe (991 cands, d2) |
| 4 | mesh: gmsh_default:overlapping_facets, gmsh_prod:other | 6 | 1 | 5 | f56dd3b77d (2011 cands, d3) |
| 5 | mesh: gmsh_default:edge_through_face, gmsh_prod:other | 5 | 1 | 4 | 4f0996fa76 (2482 cands, d3) |
| 6 | mesh: gmsh_alt:low_quality, gmsh_default:low_quality, gmsh_prod:low_quality | 4 | 2 | 2 | 722c122c9b (1308 cands, d3) |
| 7 | L1 invalid: body_overlap, self_intersection | 4 | 3 | 1 | dc2db5e786 (3407 cands, d3) |
| 8 | L1 invalid: body_overlap, brep_invalid, self_intersection | 4 | 2 | 2 | e533d000aa (3067 cands, d3) |
| 9 | mesh: gmsh_alt:low_quality, gmsh_default:edge_through_face, gmsh_prod:other | 3 | 1 | 2 | 80c009cb74 (3043 cands, d3) |
| 10 | mesh: gmsh_default:edge_through_face, gmsh_prod:other, netgen_occ:netgen_meshing_failed | 3 | 0 | 3 | 6d1c4eab6f (3011 cands, d3) |
| 11 | mesh: gmsh_default:overlapping_facets | 3 | 1 | 2 | a2807f9a2f (882 cands, d3) |
| 12 | L1 invalid: body_overlap, open_shell | 2 | 1 | 1 | 1e6f8d8e11 (3776 cands, d3) |
| 13 | mesh: gmsh_alt:low_quality, gmsh_default:overlapping_facets, gmsh_prod:low_quality | 2 | 1 | 1 | 85ab4da1c7 (739 cands, d3) |
| 14 | mesh: gmsh_default:low_quality, gmsh_prod:low_quality | 2 | 0 | 2 | b26a079a5e (1012 cands, d3) |
| 15 | mesh: gmsh_alt:periodic_surface, gmsh_default:periodic_surface, gmsh_prod:periodic_surface, netgen_occ:capped_after_doom | 2 | 2 | 0 | 96c7bdaf49 (740 cands, d3) |
| 16 | L1 invalid: unbounded_geometry | 1 | 0 | 1 | 97314ccbe4 (37 cands, d2) |
| 17 | mesh: gmsh_default:overlapping_facets, gmsh_prod:other, netgen_occ:netgen_meshing_failed | 1 | 1 | 0 | 9c6e77a796 (748 cands, d3) |
| 18 | L1 invalid: brep_invalid, open_shell, self_intersection | 1 | 1 | 0 | 782680a429 (87 cands, d1) |
| 19 | L1 invalid: body_overlap, brep_invalid | 1 | 1 | 0 | d0053d0870 (42 cands, d1) |
| 20 | mesh: gmsh_alt:low_quality, gmsh_default:overlapping_facets, gmsh_prod:other, netgen_occ:capped_after_doom | 1 | 1 | 0 | 9d0d49a626 (900 cands, d2) |
| 21 | mesh: gmsh_alt:no_volume, gmsh_default:edge_through_face, gmsh_prod:other, netgen_occ:capped_after_doom | 1 | 1 | 0 | d9b92b7973 (129 cands, d2) |
| 22 | L1 invalid: body_overlap, brep_invalid, open_shell | 1 | 1 | 0 | 68ef8f5e66 (1304 cands, d2) |
| 23 | mesh: gmsh_alt:no_volume, gmsh_default:overlapping_facets, gmsh_prod:other, netgen_occ:netgen_meshing_failed | 1 | 0 | 1 | 5f8ac4d73a (248 cands, d3) |
| 24 | mesh: gmsh_default:overlapping_facets, gmsh_prod:low_quality, netgen_occ:netgen_meshing_failed | 1 | 1 | 0 | 3e7c2a1197 (1040 cands, d2) |
| 25 | mesh: gmsh_alt:gmsh_crash, gmsh_default:low_quality, gmsh_prod:other | 1 | 1 | 0 | cb2862dd7a (1157 cands, d2) |
| 26 | mesh: gmsh_alt:timeout, gmsh_default:timeout, gmsh_prod:gmsh_crash, netgen_occ:netgen_timeout | 1 | 1 | 0 | 0522f19877 (0 cands, d0) |
| 27 | mesh: gmsh_alt:timeout, gmsh_default:overlapping_facets, gmsh_prod:other | 1 | 1 | 0 | 5accc12fa4 (1904 cands, d3) |
| 28 | mesh: gmsh_alt:no_volume, gmsh_default:low_quality, gmsh_prod:low_quality, netgen_occ:low_quality | 1 | 0 | 1 | f1eef4cd07 (1077 cands, d3) |
| 29 | L1 invalid: body_overlap, open_shell, self_intersection | 1 | 0 | 1 | 5e9f31e673 (2773 cands, d3) |
| 30 | mesh: gmsh_alt:no_volume, gmsh_default:low_quality, gmsh_prod:other, netgen_occ:netgen_meshing_failed | 1 | 0 | 1 | 0e8e2236c7 (896 cands, d3) |
| 31 | mesh: gmsh_alt:no_volume, gmsh_default:edge_through_face, gmsh_prod:other | 1 | 0 | 1 | fa37bbd567 (1422 cands, d3) |
| 32 | mesh: gmsh_alt:low_quality, gmsh_default:edge_through_face, gmsh_prod:other, netgen_occ:netgen_meshing_failed | 1 | 1 | 0 | eda22c2f6e (494 cands, d2) |
| 33 | mesh: gmsh_alt:timeout, gmsh_default:timeout, gmsh_prod:other | 1 | 1 | 0 | 36399c6239 (9 cands, d1) |
| 34 | mesh: gmsh_alt:capped_after_doom, gmsh_default:overlapping_facets, gmsh_prod:capped_after_doom, netgen_occ:capped_after_doom | 1 | 1 | 0 | 4be89ef716 (355 cands, d2) |
| 35 | mesh: gmsh_alt:low_quality, gmsh_default:low_quality | 1 | 0 | 1 | 3eb61cabb3 (1789 cands, d3) |
| 36 | mesh: gmsh_alt:no_volume, gmsh_default:no_volume, gmsh_prod:other, netgen_occ:netgen_meshing_failed | 1 | 0 | 1 | 5abbaf0b55 (69 cands, d2) |
| 37 | mesh: gmsh_default:low_quality | 1 | 0 | 1 | 7917f3122c (1193 cands, d3) |
| 38 | mesh: gmsh_alt:no_volume, gmsh_default:overlapping_facets, gmsh_prod:gmsh_crash | 1 | 0 | 1 | b13ba9c6a9 (158 cands, d2) |
| 39 | mesh: gmsh_alt:capped_after_doom, gmsh_default:overlapping_facets, gmsh_prod:other, netgen_occ:capped_after_doom | 1 | 0 | 1 | 7e573047c1 (836 cands, d3) |
| 40 | mesh: gmsh_alt:low_quality, gmsh_default:overlapping_facets, gmsh_prod:periodic_surface | 1 | 0 | 1 | 8c6a3f2186 (1666 cands, d3) |
| 41 | mesh: gmsh_prod:low_quality | 1 | 0 | 1 | a0bc551656 (1128 cands, d3) |
| 42 | L1 invalid: brep_invalid | 1 | 0 | 1 | 532be4434c (19 cands, d1) |
| 43 | mesh: gmsh_prod:other | 1 | 0 | 1 | 75e9d59b46 (895 cands, d3) |
| 44 | L1 invalid: brep_invalid, self_intersection | 1 | 1 | 0 | 32ca3e9a6e (45 cands, d1) |
| 45 | mesh: gmsh_alt:capped_after_doom, gmsh_default:edge_through_face, gmsh_prod:other, netgen_occ:netgen_meshing_failed | 1 | 0 | 1 | 557c3c0563 (881 cands, d3) |
| 46 | mesh: gmsh_alt:no_volume, gmsh_default:overlapping_facets, gmsh_prod:other, netgen_occ:step_unreadable | 1 | 0 | 1 | bb0fe0d1c8 (0 cands, d0) |
| 47 | L1 invalid: body_overlap, brep_invalid, non_positive_volume, self_intersection | 1 | 0 | 1 | affa12991b (20 cands, d1) |
| 48 | mesh: gmsh_alt:low_quality, gmsh_default:low_quality, gmsh_prod:other | 1 | 0 | 1 | 263d5e2d5b (169 cands, d2) |
| 49 | mesh: gmsh_default:overlapping_facets, gmsh_prod:low_quality, netgen_occ:capped_after_doom | 1 | 0 | 1 | 695d74ca7f (844 cands, d3) |
| 50 | mesh: gmsh_default:overlapping_facets, gmsh_prod:low_quality, netgen_occ:low_quality | 1 | 0 | 1 | e91b70f1e3 (795 cands, d3) |
| 51 | mesh: gmsh_alt:no_volume, gmsh_default:overlapping_facets, gmsh_prod:other | 1 | 0 | 1 | bc860ab1a1 (23 cands, d1) |
| 52 | mesh: gmsh_default:edge_through_face | 1 | 0 | 1 | b25999e0f8 (844 cands, d3) |
| 53 | mesh: netgen_occ:netgen_meshing_failed | 1 | 0 | 1 | 6946c8aa04 (51 cands, d2) |

## by single check (a file counts under each check that fails on its best state)
- gmsh_prod:other: 42
- gmsh_default:overlapping_facets: 32
- gmsh_alt:low_quality: 22
- L1 body_overlap: 22
- L1 self_intersection: 19
- gmsh_default:edge_through_face: 16
- gmsh_prod:low_quality: 13
- gmsh_default:low_quality: 12
- netgen_occ:netgen_meshing_failed: 11
- L1 brep_invalid: 10
- gmsh_alt:no_volume: 9
- netgen_occ:capped_after_doom: 7
- L1 open_shell: 5
- gmsh_alt:timeout: 3
- gmsh_alt:capped_after_doom: 3
- gmsh_prod:periodic_surface: 3
- gmsh_default:timeout: 2
- gmsh_prod:gmsh_crash: 2
- netgen_occ:low_quality: 2
- gmsh_alt:periodic_surface: 2
- gmsh_default:periodic_surface: 2
- L1 unbounded_geometry: 1
- gmsh_alt:gmsh_crash: 1
- netgen_occ:netgen_timeout: 1
- gmsh_prod:capped_after_doom: 1
- gmsh_default:no_volume: 1
- netgen_occ:step_unreadable: 1
- L1 non_positive_volume: 1
