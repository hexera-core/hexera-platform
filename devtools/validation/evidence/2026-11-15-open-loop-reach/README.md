# `mesh_open_loop` reach on the mesh-failure-real corpus (2026-11-15)

**Verdict: measured, and `FixFaceBoundary` was NOT extended.** On the two real open-loop files the
rules author does propose `fix_face_boundary` for the `mesh_open_loop` defect, and it clears
nothing, because the targeted face is already a valid, closed boundary: the `ShapeFix_Wire` passes
the plan proposed adding (`FixReorder`, `FixConnected`, `FixClosed`) all report "nothing to do" on
it. Adding them would be a no-op, so no code changed. `mesh_open_loop` on these files is not a
wire-topology defect the existing kernel fixers can reach.

Run: `run.json` (the `repair_corpus.py` rows). Host darwin-arm64, gmsh 4.15.2, region loop live,
probe live, `CAD_REPAIR_ENABLED/AUTONOMOUS`, planar and non-planar fill flags on, defaults otherwise:

```
CAD_REPAIR_ENABLED=true CAD_REPAIR_AUTONOMOUS=true CAD_REPAIR_FILL_PLANAR_HOLES=true \
CAD_REPAIR_FILL_NONPLANAR_HOLES=true CAD_REPAIR_MESH_PROBE=live CAD_REPAIR_REGION_LOOP=live \
python devtools/validation/repair_corpus.py <dir with the 3 files> --mesh-probe --force-repair --loop --json run.json
```

## Before / after

| file | mesh_open_loop located | fix_face_boundary proposed | kept | rolled back because | meshes after |
|---|---|---|---|---|---|
| abc/00508775_..._step_009 (1 solid, 33 faces) | yes, face 29 | yes | no | package reverted: no operation kept, target unchanged (`target_remains`) | no (still open_loop; nothing promoted) |
| abc/00509950_..._step_000 (5 solids, 2885 faces) | yes, face 2418 | yes | no | same `target_remains` | no (open_loop persists in the original; after the other regions' edits the file fails `edge_through_face` instead) |
| occt/trj10_ch1-id-chs.stp | not located (see below) | no | n/a | n/a | not measurable through the loop |

## Why the op cannot reach it

Direct kernel check on the targeted faces (both cylinders, the face passes `BRepCheck_Analyzer`):

- 00508775 face 29: `ShapeAnalysis_Wire` order, connected, closed, gaps, self-intersection, small
  and lacking checks are all clean; `ShapeFix_Wire.FixReorder/FixConnected/FixClosed/FixGaps3d/
  FixGaps2d/FixDegenerated/FixLacking` all return False; `ShapeFix_Face.Perform()` returns True but
  the face is the same shape. Every edge is `SameParameter`, tolerance at most 1.5e-4.
- 00509950 face 2418: the wire is also order/connected/closed clean. Only a 3D gap check fires, and
  that is `FixGaps3d`, which is outside the three passes planned and moves vertices
  (geometry-changing), so it was not tried as a repair. `ShapeFix_Face.Perform()` changes nothing.

So the 1D-mesh "does not form a closed loop" result is a property gmsh derives from this geometry
(tessellating an edge loop on the cylinder), not a defect the wire fixers can see. Further work would
need a different diagnosis (what gmsh's 1D discretisation does with these edges), not more
`ShapeFix_Wire` passes.

## Residuals, honestly

- **trj10 is a reader effect.** `occt/trj10_ch1-id-chs.stp` fails as `open_loop` when gmsh reads the
  ORIGINAL file (the file-probe arm, ground truth in the calibration), but reads clean under OCP and
  the OCP re-export meshes (calibration: file `open_loop`, reexport and loop `meshes`). The region
  loop therefore never sees a `mesh_open_loop` defect on it; in this run it saw only a
  `small_edge` and refused with "nothing was kept". Whether the repair pipeline could reach it
  depends on the delivered STEP being the re-export, which is a different question from this one.
- The 00509950 row is dominated by 589 small faces and 51 small edges from other regions, so its
  "after" kind (`edge_through_face`) is not attributable to the open-loop target.
- Two files is the whole population: the corpus manifest names four 1D-loop failures
  (`trj3_s1-id-203` is volume-stage `edge_through_face` under OCP, so it is not an open-loop case).
- `--force-repair` is a benchmark switch; the product triage routes a clean-inspecting file to
  mesh-as-is.
