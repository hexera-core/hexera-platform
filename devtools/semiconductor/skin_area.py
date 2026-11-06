#!/usr/bin/env python3
"""PHASE 0c: what the staircase does to the plate's OUTER skin.

The plate-only mesh used `block.stl` as its own `surfaceFile`, so cfMesh fitted the outer envelope
to the plate. The coupled mesh is built on a box domain instead, so the plate's outer surface is a
staircase cut out of the octree. `block.stl` is the fitted target and its area is exact, so the
inflation is measurable off the sidecar alone, with no second mesh to build.

`block.stl` encloses metal AND coolant: its volume matches the meshed plate region plus the coolant
to 0.065%, and it carries no internal channel walls. So its area is purely the outer skin, and the
mesh's outer skin is solid_to_air plus solid_to_chip, every plate face not wetted by coolant.

Areas are exact for planar polygons, by the 3D shoelace: A = 0.5 * sum(v_i x v_i+1) round each
face's loop. No assumption that a face is a quad, because 0.19% of them are not.

  usage: skin_area.py <solid boundaryIndex dir> <block.stl>
"""
import json
import sys

import numpy as np

B = sys.argv[1]
STL = sys.argv[2]


def rd(name, dt):
    return np.fromfile(f"{B}/{name}", dtype=dt)


meta = json.loads(open(f"{B}/meta.json").read())
sizes = rd("sizes.u8", np.uint8).astype(np.int64)
conn = rd("conn.u32", np.uint32).astype(np.int64)
pts = rd("points.f32", np.float32).reshape(-1, 3).astype(np.float64)
patch_of = rd("patch_of.u16", np.uint16)
offs = np.concatenate(([0], np.cumsum(sizes)))
start = offs[:-1]
print(f"faces {sizes.size:,}   corners {conn.size:,}   points {pts.shape[0]:,}"
      f"   quads {int((sizes == 4).sum()):,}   non-quads {int((sizes != 4).sum()):,}")

# the loop successor of every corner: the next one, except the last in a face, which wraps
nxt = np.arange(1, conn.size + 1)
nxt[offs[1:] - 1] = start
v = pts[conn]
avec = 0.5 * np.add.reduceat(np.cross(v, v[nxt]), start, axis=0)
area = np.linalg.norm(avec, axis=1)
del v, avec

names = {i: p["name"] for i, p in enumerate(meta.get("patches") or [])}
tot = {}
print("\narea per patch, from the index:")
for pid in np.unique(patch_of):
    if int(pid) == np.iinfo(np.uint16).max:
        continue
    m = patch_of == pid
    a = area[m].sum() * 1e6
    tot[names.get(int(pid), str(pid))] = a
    print(f"   {names.get(int(pid), pid):18s} {int(m.sum()):>12,} faces   {a:14.3f} mm2")

b = np.fromfile(STL, dtype=np.uint8)
n = int(np.frombuffer(b[80:84].tobytes(), dtype="<u4")[0])
rec = b[84:].reshape(n, 50)
tri = np.frombuffer(np.ascontiguousarray(rec[:, 12:48]).tobytes(),
                    dtype="<f4").reshape(n, 3, 3).astype(np.float64)
stl_area = (0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]),
                                 axis=1)).sum() * 1e6

outer = sum(v for k, v in tot.items() if "air" in k or "chip" in k)
wetted = sum(v for k, v in tot.items() if "fluid" in k)
print(f"\n{STL.rsplit('/', 1)[-1]}: {n:,} triangles, envelope area {stl_area:.3f} mm2"
      "   (the fitted target)")
print(f"plate outer skin in the mesh, to air plus to chip : {outer:14.3f} mm2")
print(f"plate wetted by coolant                           : {wetted:14.3f} mm2")
print(f"\nSTAIRCASE ON THE OUTER SKIN: {100.0*(outer/stl_area - 1):+.2f}%")
