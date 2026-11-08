#!/usr/bin/env python3
"""Pick the production cell size by computing, not by running a 3-hour mesh and looking.

THE MECHANISM. splitMeshRegions cuts along existing octree cell faces, so a channel wall can only
land on a cell face and the channel width is forced to a whole number of cells. On top of that the
mesher does not see the CAD width: the staircased wall sits a fixed distance OUTWARD of the true
wall, so the width it quantises is w_cad + 2b for a per-wall bias b. That b is a LENGTH, so in cell
units it grows as the cell shrinks, which is why finer cells make the off-target share worse.

An inward offset o applied to the STL cancels it: the mesher then sees w_cad + 2(b - o).

WHAT THIS DECIDES. Total flow bias is the product of two independent terms:
  (a) discretisation, exactly 1 + 2/N^2 for N cells across, verified to ratio 1.000000
  (b) quantisation, mean(w_mesh^3)/mean(w_cad^3), because slot conductance goes as w^3
(a) is clean, uniform and exactly correctable. (b) varies channel to channel and redistributes
flow between branches, which is the one thing a topology-optimised plate cannot tolerate. So the
right objective is to minimise (b) and hand (a) over as a stated correction.
"""
from __future__ import annotations

import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
STL = os.path.join(HERE, "mesh", "fluid.stl")

PITCH = 201.26253e-6          # measured, core n=103,980, CV 0.383%
NOMINAL = 100.6315e-6         # the CAD channel the array is built around


def load_binary_stl(path):
    with open(path, "rb") as f:
        raw = f.read()
    n = struct.unpack("<I", raw[80:84])[0]
    if 84 + 50 * n != len(raw):
        sys.exit("not a binary STL of the expected length")
    rec = np.frombuffer(raw, dtype=np.dtype([
        ("n", "<3f4"), ("v", "<3,3f4"), ("attr", "<u2")]), count=n, offset=84)
    return rec["v"].astype(np.float64)       # (n, 3 verts, 3 coords)


def ray_gaps(tris, y0, z0):
    """Fluid segments along +x through (y0, z0). Axis-aligned, so exact in the y-z projection."""
    ay, az = tris[:, 0, 1], tris[:, 0, 2]
    by, bz = tris[:, 1, 1], tris[:, 1, 2]
    cy, cz = tris[:, 2, 1], tris[:, 2, 2]
    d = (by - ay) * (cz - az) - (bz - az) * (cy - ay)
    ok = np.abs(d) > 1e-20
    wa = ((by - y0) * (cz - z0) - (bz - z0) * (cy - y0))
    wb = ((cy - y0) * (az - z0) - (cz - z0) * (ay - y0))
    wc = ((ay - y0) * (bz - z0) - (az - z0) * (by - y0))
    inside = ok & (((wa >= 0) & (wb >= 0) & (wc >= 0)) | ((wa <= 0) & (wb <= 0) & (wc <= 0)))
    if not inside.any():
        return np.empty(0)
    t = tris[inside]
    dd = d[inside]
    l0, l1, l2 = wa[inside] / dd, wb[inside] / dd, wc[inside] / dd
    x = l0 * t[:, 0, 0] + l1 * t[:, 1, 0] + l2 * t[:, 2, 0]
    x = np.sort(x)
    # a closed surface alternates out->in, in->out, so pairs are the inside segments
    if x.size % 2:
        return np.empty(0)                   # grazed a vertex or edge; drop the ray
    return x[1::2] - x[0::2]


def cad_widths(tris, nrays=4000, seed_grid=True):
    ymin, ymax = tris[:, :, 1].min(), tris[:, :, 1].max()
    zmin, zmax = tris[:, :, 2].min(), tris[:, :, 2].max()
    # the coolant band found by probing the real mesh: y about 21.6 to 28 mm
    ylo, yhi = max(ymin + 2e-4, 0.0220), min(ymax - 2e-4, 0.0280)
    ny = int(np.sqrt(nrays * (yhi - ylo) / (zmax - zmin))) + 1
    nz = max(1, nrays // max(1, ny))
    ys = np.linspace(ylo, yhi, ny)
    zs = np.linspace(zmin + 2e-4, zmax - 2e-4, nz)
    out, nray_ok = [], 0
    for y0 in ys:
        for z0 in zs:
            g = ray_gaps(tris, y0, z0)
            if g.size:
                nray_ok += 1
                out.append(g)
    if not out:
        sys.exit("no rays hit the surface")
    w = np.concatenate(out)
    return w, nray_ok


def main():
    tris = load_binary_stl(STL)
    print("fluid.stl: %d triangles" % len(tris))
    bb = [(tris[:, :, i].min(), tris[:, :, i].max()) for i in range(3)]
    print("bbox mm: x %.3f..%.3f  y %.3f..%.3f  z %.3f..%.3f"
          % tuple(v * 1e3 for p in bb for v in p))

    w, nray = cad_widths(tris)
    print("\nrays that hit cleanly: %d, gaps found: %d" % (nray, w.size))

    # THE ARRAY FILTER. Unfiltered gaps include plenum and header crossings, which are hundreds of
    # microns to millimetres and would swamp the channel statistics. The record says this exact
    # mistake produced a 55.8% flow CV that was a measurement artefact.
    band = (w > 60e-6) & (w < 180e-6)
    ch = w[band]
    print("gaps inside the 60-180 um channel band: %d (%.1f%% of all gaps)"
          % (ch.size, 100.0 * ch.size / w.size))
    print("\nCAD channel width, from this raycast")
    for q in (5, 25, 50, 75, 95):
        print("   p%-3d %9.4f um" % (q, np.percentile(ch, q) * 1e6))
    print("   mean %9.4f um   CV %.2f%%" % (ch.mean() * 1e6, 100 * ch.std() / ch.mean()))
    print("   the record's CAD p50 is 100.446 um; agreement here validates the filter")

    # CALIBRATE the per-wall bias b from the ONE measured mesh we have.
    # cbox2b: dx 20.1263 um, measured mean 5.1036 cells across.
    dx_ref, mean_cells_ref = 20.1263e-6, 5.1036
    b = (mean_cells_ref * dx_ref - ch.mean()) / 2.0
    print("\ncalibrated per-wall staircase bias b = %.4f um" % (b * 1e6))
    print("   (from cbox2b's measured mean of %.4f cells at dx %.4f um)"
          % (mean_cells_ref, dx_ref * 1e6))

    w3_cad = (ch ** 3).mean()

    def evaluate(dx, offset):
        seen = ch + 2.0 * (b - offset)
        n = np.maximum(1, np.rint(seen / dx))
        wm = n * dx
        n_nom = round(NOMINAL / dx)
        return {
            "on_nominal": 100.0 * (n == n_nom).mean(),
            "cond": (wm ** 3).mean() / w3_cad,
            "cells_across": NOMINAL / dx,
            "n_nom": n_nom,
            "disc": 1.0 + 2.0 / (n_nom ** 2),
        }

    # cell count and time scale as dx^-3 off the one build we measured
    CELLS_REF, MESH_S_REF, CHAIN_S_REF, LAYER_S_REF = 504.7e6, 618, 1852, 5843

    print("\n" + "=" * 100)
    print("COMMENSURATE CELL SIZES.  dx = pitch/k, so the channel lands on a whole count by")
    print("construction. ODD k is useless: the channel then lands on a half cell.")
    print("=" * 100)
    print("%-6s %10s %7s %9s %9s %9s %9s %10s %9s"
          % ("pitch", "dx um", "N", "cells", "mesh s", "chain s", "+layer s", "on-nom %", "TOTAL bias"))
    rows = []
    for k in (6, 8, 10, 12):
        dx = PITCH / k
        e = evaluate(dx, 0.0)
        s = (dx_ref / dx) ** 3
        cells = CELLS_REF * s
        tot = e["disc"] * e["cond"]
        rows.append((k, dx, e, cells, tot))
        print("%-6d %10.4f %7d %9.0fM %9.0f %9.0f %9.0f %10.1f %+9.1f%%"
              % (k, dx * 1e6, e["n_nom"], cells / 1e6, MESH_S_REF * s, CHAIN_S_REF * s,
                 LAYER_S_REF * s, e["on_nominal"], 100 * (tot - 1)))
    print("\n   N is cells across the channel. 'cells' and the times scale as dx^-3 off cbox2b")
    print("   (504.7M, mesh 618 s, full chain 1,852 s, layer pass 5,843 s).")
    print("   TOTAL bias = (1 + 2/N^2) x quantisation, i.e. how much too much coolant flows.")
    print("   The 600-650M cell ceiling (2^31 faces at 3.02 faces/cell) rules out pitch 12.")

    print("\n" + "=" * 100)
    print("THE FREE LEVER: an inward offset on the STL, which cancels the staircase bias")
    print("=" * 100)
    print("%-6s %8s %10s %10s %12s %12s" % ("pitch", "dx um", "offset um", "on-nom %",
                                            "quant bias", "TOTAL bias"))
    best = None
    for k in (8, 10):
        dx = PITCH / k
        for o_um in (0.0, 0.5, 0.75, 1.0, 1.045, 1.25, 1.5):
            e = evaluate(dx, o_um * 1e-6)
            tot = e["disc"] * e["cond"]
            star = ""
            if best is None or abs(e["cond"] - 1) < abs(best[2]["cond"] - 1):
                best = (k, o_um, e, tot)
            print("%-6d %8.4f %10.3f %10.1f %+11.2f%% %+11.1f%%"
                  % (k, dx * 1e6, o_um, e["on_nominal"], 100 * (e["cond"] - 1), 100 * (tot - 1)))
        print()
    k, o_um, e, tot = best
    print("LOWEST QUANTISATION BIAS: pitch %d, dx %.4f um, offset %.3f um"
          % (k, PITCH / k * 1e6, o_um))
    print("   on nominal %.1f%%, quantisation %+0.2f%%, discretisation %+0.1f%%, total %+0.1f%%"
          % (e["on_nominal"], 100 * (e["cond"] - 1), 100 * (e["disc"] - 1), 100 * (tot - 1)))
    s = (dx_ref / (PITCH / k)) ** 3
    print("   predicted %.0fM cells, chain %.0f s, layer pass %.0f s, TOTAL %.2f h"
          % (CELLS_REF * s / 1e6, CHAIN_S_REF * s, LAYER_S_REF * s,
             (CHAIN_S_REF + LAYER_S_REF) * s / 3600))


if __name__ == "__main__":
    main()
