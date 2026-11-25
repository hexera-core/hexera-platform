# Lane D17: gmsh_prod / Netgen failures on valid dev files - which member, where, why (diagnosis only)

Probe (`lane-d17/d17_diag.py`): 86 VALID dev files (v2 + v1r splits, deduplicated) whose label fails gmsh_prod or
netgen_occ. Engine form; gmsh_prod and netgen_occ run exactly as the full panel runs them; located failures placed
on workspace faces with `find_sites` (gmsh_prod's boxes de-padded, as fixed at .5); every located face and the part
measured (surface type, periodic/seam, poles, shortest edge vs gmsh_prod's element size h = diag/10, sharpest
interior angle at its edges, near-tangent edges). Read-only. Per-file table: `lane-d17/table_out.md`; rows in
`lane-d17/rows/`. 85 of 86 ran (1 error).

## By member and kind
| member:kind | files | located | located faces with an edge < 0.1 h | < 0.01 h | periodic | pole | sharp < 10 deg | free-form |
|---|---|---|---|---|---|---|---|---|
| gmsh_prod: other = "HXT 3D mesh failed" (54/54) | 54 | 51 | 26 | 12 | 38 | 0 | 0 | 5 |
| gmsh_prod: low_quality (min SICN < 0.01) | 27 | 27 | 24 | 12 | 21 | 0 | 3 | 4 |
| netgen_occ: netgen_meshing_failed | 9 | 1 | 1 | 0 | 1 | 0 | 0 | 0 |
| netgen_occ: step_unreadable ("STEP file holds no solids") | 5 | 0 | - | - | - | - | - | - |
| netgen_occ: low_quality | 5 | 0 | - | - | - | - | - | - |
| netgen_occ: netgen_timeout (570 s) | 4 | 0 | - | - | - | - | - | - |
| gmsh_prod: periodic_surface ("Impossible to mesh periodic surface 1") | 1 | 1 | 1 | 1 | 1 | 0 | 0 | 0 |
| gmsh_prod: gmsh_crash / netgen_occ: netgen_crash | 1 / 1 | 0 | - | - | - | - | - | - |

## What the evidence says
1. **Periodic/seam faces are NOT enriched at the failure.** Located faces are periodic 41% on average against 42%
   of their own part's faces (more periodic on 27 files, less on 40, same on 12). A site-level `split_closed` is
   not indicated by this data; the one `periodic_surface` file is a cylinder whose shortest edge is < 0.01 h.
2. **No poles, almost no sharp angles.** 0 of 51 HXT files and 3 of 27 low-quality files have a located face with
   an interior angle < 10 deg; none has a degenerate edge. `split_angle` at the site is not indicated either.
3. **Small features against the production element size are the common property.** Half the HXT files (26/51)
   and nearly all low-quality files (24/27) have a located face with an edge under a tenth of gmsh_prod's element
   size; 12 and 12 under a hundredth. But this is a weak signal on its own: the part's SHORTEST edge sits on a
   located face in only 33 of 79 files, and parts have short edges everywhere (median part min edge 0.009 h).
4. **"HXT 3D mesh failed" carries no geometric reason** in 54 of 54 messages. Several located HXT faces are
   plain planes and cylinders with edges near h (db90265b9b: 0.69 h). That fits HXT failing on the 2D surface
   mesh it is handed (intersecting boundary triangles at a coarse size) rather than on a named defect.
5. **Netgen locates almost nothing** (1 of 24 failing files). And 5 VALID files fail Netgen with "STEP file holds
   no solids" on the engine-form STEP: a reading problem, not geometry.

## Not measured (next, before building)
No control: the same flags were not measured on files gmsh_prod PASSES, so "short edge" is not yet shown to be a
cause rather than common. Two cheap experiments would settle what to aim at: (a) gmsh_prod on the located faces
with their short edges removed (`remove_small` / located `defeature` at the site) - does HXT then pass; (b) whether
HXT passes at h/2 (resolution-bound failures cannot be fixed by geometry ops, only by removing the small features).
