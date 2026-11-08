#!/usr/bin/env python3
"""Turn the extracted window of the LAYERED coolant into a drawable cross-section.

The window is a slab two cells thick in z, cut straight out of the 143,236,900-cell layered fluid
region with topoSet + subsetMesh. Its boundary faces split cleanly by normal:
  |nz| ~ 1   the two cut faces, which tile the channel cross-sections. This is the picture.
  nz   ~ 0   the real wetted wall, i.e. the conjugate interface itself.
Drawing the cut faces gives a true picture of the mesh in section: every polygon is one real cell,
so the cells across the channel and the boundary-layer stack are the mesh's own, not a sketch.
"""
from __future__ import annotations

import json
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PM = os.path.join(HERE, "mesh", "0.001", "polyMesh")
OUT = os.path.join(HERE, "section.json")


def body(path):
    """Strip the FoamFile header and return the token stream after it."""
    with open(path, "r", errors="replace") as f:
        t = f.read()
    i = t.find("// * * *")
    i = t.find("\n", i) if i >= 0 else 0
    return t[i:]


def read_points(path):
    t = body(path)
    m = re.search(r"(\d+)\s*\(", t)
    n = int(m.group(1))
    chunk = t[m.end():]
    vals = np.fromstring(chunk.replace("(", " ").replace(")", " ")[: chunk.rfind(")")],
                         sep=" ", dtype=np.float64)
    return vals[: 3 * n].reshape(n, 3)


def read_faces(path):
    t = body(path)
    m = re.search(r"(\d+)\s*\(", t)
    n = int(m.group(1))
    out, pos = [], m.end()
    pat = re.compile(r"(\d+)\s*\(([^)]*)\)")
    for _ in range(n):
        mm = pat.search(t, pos)
        if not mm:
            break
        out.append([int(x) for x in mm.group(2).split()])
        pos = mm.end()
    return out


def read_labels(path):
    t = body(path)
    m = re.search(r"(\d+)\s*\(", t)
    n = int(m.group(1))
    chunk = t[m.end():]
    return np.fromstring(chunk[: chunk.rfind(")")], sep=" ", dtype=np.int64)[:n]


def read_boundary(path):
    t = body(path)
    out = {}
    for mm in re.finditer(r"(\w+)\s*\{([^}]*)\}", t):
        b = mm.group(2)
        nf = re.search(r"nFaces\s+(\d+)\s*;", b)
        sf = re.search(r"startFace\s+(\d+)\s*;", b)
        ty = re.search(r"type\s+(\w+)\s*;", b)
        if nf and sf:
            out[mm.group(1)] = (int(sf.group(1)), int(nf.group(1)),
                                ty.group(1) if ty else "?")
    return out


def main():
    pts = read_points(os.path.join(PM, "points"))
    faces = read_faces(os.path.join(PM, "faces"))
    owner = read_labels(os.path.join(PM, "owner"))
    bnd = read_boundary(os.path.join(PM, "boundary"))
    nint = len(owner) - sum(v[1] for v in bnd.values())
    print("points %d  faces %d  cells %d  internal %d" % (len(pts), len(faces),
                                                          owner.max() + 1, nint))
    for k, (s, n, ty) in bnd.items():
        print("   patch %-16s %8d faces  %s" % (k, n, ty))

    # normals and centroids of every boundary face
    cut, wall = [], []
    for fi in range(nint, len(faces)):
        idx = faces[fi]
        p = pts[idx]
        # Newell normal, exact for a planar polygon of any vertex count
        nrm = np.zeros(3)
        for a in range(len(p)):
            q, r = p[a], p[(a + 1) % len(p)]
            nrm += np.cross(q, r)
        area = np.linalg.norm(nrm) / 2.0
        if area <= 0:
            continue
        nz = abs(nrm[2]) / (2.0 * area)
        (cut if nz > 0.9 else wall).append((fi, area))

    print("\ncut faces (|nz|~1), the cross-section: %d" % len(cut))
    print("wetted wall faces in the window:        %d" % len(wall))

    # keep one of the two cut planes: the one at the lower z
    zc = np.array([pts[faces[fi]][:, 2].mean() for fi, _ in cut])
    keep = [cut[i][0] for i in np.where(zc < zc.mean())[0]]
    print("one cut plane: %d cells in section" % len(keep))

    polys, xs, ys = [], [], []
    for fi in keep:
        p = pts[faces[fi]]
        polys.append([[round(float(v[0]) * 1e6, 4), round(float(v[1]) * 1e6, 4)] for v in p])
        xs.extend(p[:, 0]); ys.extend(p[:, 1])
    xs, ys = np.array(xs), np.array(ys)

    # cell size histogram across x, which is the channel-width direction
    widths = np.array([max(q[0] for q in pl) - min(q[0] for q in pl) for pl in polys])
    heights = np.array([max(q[1] for q in pl) - min(q[1] for q in pl) for pl in polys])
    print("\nin-section cell size, um")
    print("   x: min %.4f  p50 %.4f  max %.4f" % (widths.min(), np.median(widths), widths.max()))
    print("   y: min %.4f  p50 %.4f  max %.4f" % (heights.min(), np.median(heights), heights.max()))
    thin = widths < 0.5 * np.median(widths)
    print("   cells thinner than half the median in x: %d (%.1f%%)  <- the layer stack"
          % (thin.sum(), 100.0 * thin.mean()))

    data = {
        "polys": polys,
        "bounds": {"x0": float(xs.min() * 1e6), "x1": float(xs.max() * 1e6),
                   "y0": float(ys.min() * 1e6), "y1": float(ys.max() * 1e6)},
        "n_section_cells": len(polys),
        "n_wall_faces": len(wall),
        "cell_x_median_um": float(np.median(widths)),
        "cell_x_min_um": float(widths.min()),
        "cell_y_median_um": float(np.median(heights)),
        "layer_cells": int(thin.sum()),
    }
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print("\nwrote %s  (%.2f MB)" % (OUT, os.path.getsize(OUT) / 1e6))
    print("window spans x %.1f..%.1f um, y %.1f..%.1f um"
          % (data["bounds"]["x0"], data["bounds"]["x1"],
             data["bounds"]["y0"], data["bounds"]["y1"]))


if __name__ == "__main__":
    main()
