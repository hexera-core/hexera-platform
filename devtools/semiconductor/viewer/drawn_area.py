#!/usr/bin/env python3
"""Did the decimation LOSE surface, or is it only coarse?

The gaps in the viewer could be two very different things. Either the reduction is dropping
geometry, in which case the picture is wrong and nobody should trust it; or it is merely coarser
than the channel pitch, in which case the surface is all there and simply cannot resolve a
100.631 um channel with a 446 um cluster.

The test is area. checkMesh's own wetted area for fluid_to_solid is 63,763.58 mm2, measured off
the real mesh. If the drawn surface comes to about that, nothing was lost and the gaps are the
cluster size. If it is well short, faces went missing.
"""
import base64
import json
import pathlib
import sys

import numpy as np

TRUTH_MM2 = 63763.58           # fluid_to_solid, measured on the delivered mesh
P = pathlib.Path("/home/rehaan/corintis_view/deliver_viewer_data.json")

d = json.loads(P.read_text())
surf = d.get("surface") or d
print("payload %.1f MB" % (P.stat().st_size / 1e6))

for patch in surf.get("patches") or []:
    name = patch.get("name")
    pts = np.frombuffer(base64.b64decode(patch["points_b64"]), dtype="<f4").reshape(-1, 3)
    polys = np.frombuffer(base64.b64decode(patch["polys_b64"]), dtype="<u4")
    # walk the packed [n, i0..in-1, ...] stream and fan each polygon from its first vertex
    area = 0.0
    tris = 0
    i = 0
    while i < polys.size:
        n = int(polys[i])
        if n < 3:
            break
        idx = polys[i + 1: i + 1 + n]
        v0 = pts[idx[0]]
        for k in range(1, n - 1):
            a = pts[idx[k]] - v0
            b = pts[idx[k + 1]] - v0
            area += 0.5 * float(np.linalg.norm(np.cross(a, b)))
            tris += 1
        i += n + 1
    mm2 = area * 1e6
    line = "  %-26s %9s polys %9s tris  area %12.2f mm2" % (
        name, f"{0:,}", f"{tris:,}", mm2)
    print(line)
    if name and name.endswith("fluid_to_solid"):
        print("      against checkMesh's own wetted area %12.2f mm2  -> %+.2f%%"
              % (TRUTH_MM2, 100.0 * (mm2 - TRUTH_MM2) / TRUTH_MM2))
        print("      channel width 100.631 um, cluster used 446 um -> %.1f channels per cluster"
              % (446.0 / 100.631))
