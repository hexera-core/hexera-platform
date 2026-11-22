#!/usr/bin/env python3
"""Prove the decomposed delivery end to end on a SMALL mesh, before spending an hour on a big one.

WHAT THIS IS FOR. Every path added in edits 7-11 runs only when a conjugate build is parallel AND
not merged, and nothing in the suite can run OpenFOAM. So the first real exercise of that code
would otherwise be an hour of VM time on the cold plate, where a refusal arrives at the end and
costs the whole run. This builds a small two-region mesh with `merge=False`, then walks everything
that reads the result:

    build(ranks=N, merge=False)   the chain, with no reconstructParMesh
      -> _publish_decomposed_boundaries   the regions described from the ranks
      -> checkMesh across the ranks       the verdict and the three quality fields
      -> write_index_decomposed           the boundary index, from processor*/
      -> write_quality_decomposed         the colours, numbered against that index
      -> _patches_from_index              the surface the reviewer opens

It runs inside the mesh image with /work/src on PYTHONPATH, so it is THIS checkout's code against
the real OpenFOAM.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, "/work/devtools/semiconductor")

import parts as P  # noqa: E402
import semi  # noqa: E402

from meshpipeline.engines.cfmesh_conjugate import conjugate as C  # noqa: E402
from meshpipeline.engines.cfmesh_conjugate import native  # noqa: E402
from meshpipeline.engines.cfmesh_conjugate.polymesh_surface import (  # noqa: E402
    decomposed_region_boundary,
    processor_dirs,
)

ap = argparse.ArgumentParser()
ap.add_argument("--part", default="unit_cell_100um")
ap.add_argument("--across", type=int, default=2)
ap.add_argument("--levels", type=int, default=3)
ap.add_argument("--ranks", type=int, default=2)
ap.add_argument("--layers", type=int, default=0)
ap.add_argument("--merge", default="0")
a = ap.parse_args()
MERGE = str(a.merge).strip().lower() not in {"0", "false", "no", "off"}

part = semi.pick(semi.CAD, a.part)
lo, hi, background, open_, ports, cells, nbytes = semi.plan(
    part, a.across, a.levels, None, None, None, False)

ws = Path("/work/runs") / f"prove-{a.part}-{a.across}across-{a.ranks}r-merge{int(MERGE)}"
if ws.exists():
    shutil.rmtree(ws)
(ws / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
semi.write_case_skeleton(ws)
(ws / "constant" / "triSurface" / "block.stl").write_text(C.block_surface_stl(lo, hi))
shutil.copyfile(part.fluid_stl, ws / "constant" / "triSurface" / "fluid.stl")

print(f"part        {part.name}")
print(f"across      {a.across}   levels {a.levels}   background {background * 1e6:.3f} um")
print(f"ranks       {a.ranks}    layers {a.layers}    merge {MERGE}")
print(f"estimate    {cells:,} cells")
print(f"workspace   {ws}")
print()

t0 = time.monotonic()
rep = C.build(ws, background_cell_m=background, levels=a.levels,
              expect_fluid_volume_m3=part.fluid_volume_m3,
              expect_fluid_pieces=part.passages,
              volume_tol=semi.volume_tolerance(a.across, None)[0],
              ports=ports, narrowest_m=part.narrowest_m,
              ranks=int(a.ranks), layers=int(a.layers), merge=MERGE)
build_s = time.monotonic() - t0

print()
print("=" * 78)
print("THE CHAIN")
print("=" * 78)
for k, v in (rep.seconds or {}).items():
    print(f"  {k:<36} {v:>10.1f} s")
print(f"  {'TOTAL (wall, this process)':<36} {build_s:>10.1f} s")
print()
print(f"  fluid  {rep.fluid.cells:>14,} cells   mesh_ok={rep.fluid.mesh_ok}  fatal={rep.fluid.fatal}")
print(f"  solid  {rep.solid.cells:>14,} cells   mesh_ok={rep.solid.mesh_ok}  fatal={rep.solid.fatal}")
print(f"  interface {rep.interface_faces_fluid:,} / {rep.interface_faces_solid:,} faces")
if rep.problems:
    print(f"  problems: {rep.problems}")
if rep.layers:
    print(f"  layers: {json.dumps(rep.layers)[:400]}")

print()
print("=" * 78)
print("WHAT THE DELIVERY LOOKS LIKE")
print("=" * 78)
ranks = processor_dirs(ws)
print(f"  processor dirs kept      {len(ranks)}")
for region in ("fluid", "solid"):
    pm = ws / "constant" / region / "polyMesh"
    have = sorted(p.name for p in pm.iterdir()) if pm.is_dir() else []
    cells_here = "faces" in have
    print(f"  constant/{region}/polyMesh  {'has a serial mesh' if cells_here else 'patch table only'}"
          f"  ({len(have)} files)")
    if ranks and not cells_here:
        rows = decomposed_region_boundary(ws, region)
        print(f"      ranks declare {sum(int(r['n_faces']) for r in rows):,} real boundary faces "
              f"over {len(rows)} patches")

print()
print("=" * 78)
print("THE SIDECARS, through the engine's own entry point")
print("=" * 78)
t1 = time.monotonic()
idx = native._boundary_indexes(ws)
print(f"  _boundary_indexes took {time.monotonic() - t1:.1f} s")
for name, got in (idx.get("regions") or {}).items():
    if not got.get("ok"):
        print(f"  {name:<8} INDEX FAILED: {got.get('why')}")
        continue
    q = got.get("quality") or {}
    print(f"  {name:<8} ok  decomposed={got.get('decomposed')}  "
          f"{got['faces']:,} faces  {got['points']:,} points  "
          f"{got['bytes'] / 1e6:.1f} MB  {got['seconds']}s")
    if q.get("ok"):
        print(f"           colours {q.get('metrics')}  {q.get('bytes', 0) / 1e6:.1f} MB  "
              f"ranks={q.get('ranks')}  cleared {q.get('fields_cleared_bytes', 0) / 1e6:.1f} MB")
        g = q.get("checked_against_checkMesh") or {}
        for kk, vv in g.items():
            print(f"           gate {kk}: {vv}")
        if q.get("notes"):
            print(f"           notes {q['notes']}")
    else:
        print(f"           NO COLOURS: {q.get('why')}")

print()
print("=" * 78)
print("THE SURFACE THE REVIEWER OPENS")
print("=" * 78)
from meshpipeline.engines.cfmesh_conjugate.review_surface import (  # noqa: E402
    _patches_from_index,
)
for region in ("fluid", "solid"):
    pts = _patches_from_index(ws, region)
    n = sum(int(p.get("face_count") or 0) for p in pts)
    print(f"  {region:<8} {len(pts)} patches, {n:,} faces  "
          f"{[p['name'] for p in pts][:6]}")

print()
print("=" * 78)
print("AND THE DETAIL WINDOW, which is what the customer clicks")
print("=" * 78)
from meshpipeline.render.detail_window import delivered_regions, detail_at  # noqa: E402
print(f"  delivered_regions: {[n for n, _ in delivered_regions(ws)]}")
mid = [(lo[i] + hi[i]) / 2 for i in range(3)]
try:
    win = detail_at(ws, centre=tuple(mid), radius=float(background) * 8)
    print(f"  centre {win.get('centre')}  radius {win.get('radius')}")
    print(f"  faces in range {win.get('faces_in_range', 0):,}, "
          f"returned {win.get('faces_returned', 0):,}, truncated={win.get('truncated')}")
    for pch in (win.get("patches") or []):
        q = [k for k in ("skewness", "non_ortho", "aspect_ratio") if pch.get(k) is not None]
        print(f"    {str(pch.get('name')):<26} {int(pch.get('face_count') or 0):>8,} faces"
              f"  colours={q}")
except Exception as exc:                      # noqa: BLE001
    print(f"  detail_at RAISED: {type(exc).__name__}: {exc}")

print()
print("DONE.", "merged" if MERGE else "DECOMPOSED - no reconstructParMesh ran")
