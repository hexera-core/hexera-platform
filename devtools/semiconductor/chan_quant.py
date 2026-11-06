#!/usr/bin/env python3
"""Measure the channel widths a coupled mesh actually produced, off the boundary index.

WHY THIS EXISTS. The conjugate interface lies on existing octree cell faces: splitMeshRegions cuts
along cell faces and nothing fits a surface on an internal boundary. So a coolant channel comes out
a whole number of cells wide, and if no whole number lands on the width the part is built to, the
mesh cannot represent it. On the Corintis plate at 22.0035 um that turned one 100.04 um channel
population into three, 88 / 110 / 132 um, chosen by where each channel happened to fall on the
grid. Slot flow goes as width cubed, so two channels the CAD made identical came out 1.98x apart.
Full story in docs/semiconductor/MESH_QUANTISATION.md.

Raycasting the CAD cannot find this, because it can only ever return the CAD's own width. The
measurement has to come off the mesh.

HOW. Read the fluid_to_solid face centres and corners out of the boundary index sidecar, recover
each face's outward normal from its winding, keep the axis-aligned x-normal quads in a thin slab at
mid channel depth, group them into scan lines by (y, z), and pair each inward-facing wall with the
next outward-facing one along the line. Every face in the slab is an axis-aligned quad, so no
approximation enters.

The run reports twice. The full histogram covers every gap, including the wider manifold and plenum
channels that feed the array and are genuinely different widths. The core band covers only the
microchannel array, so the manifold is not counted as scatter. Give the band from the CAD, not from
the mesh.

  usage: chan_quant.py <boundaryIndex dir> <dx metres> <label> [core_lo_um core_hi_um] [y0 y1]

  e.g.  chan_quant.py /mnt/work/cbox2b/constant/fluid/polyMesh/boundaryIndex 2.012625e-05 cbox2b 70 132

Needs PYTHONPATH pointing at the repo src only if the index has to be built first; reading one
needs numpy and nothing else.
"""
import json
import sys

import numpy as np

if len(sys.argv) < 4:
    sys.exit(__doc__)
B = sys.argv[1]
DX = float(sys.argv[2])
LAB = sys.argv[3]
CORE = (float(sys.argv[4]), float(sys.argv[5])) if len(sys.argv) > 5 else None
Y0 = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0222
Y1 = float(sys.argv[7]) if len(sys.argv) > 7 else 0.0224


def rd(name, dt):
    return np.fromfile(f"{B}/{name}", dtype=dt)


def gaps():
    """Every channel gap in the slab, in microns."""
    sizes = rd("sizes.u8", np.uint8)
    cent = rd("centroids.f32", np.float32).reshape(-1, 3)
    patch_of = rd("patch_of.u16", np.uint16)
    print(f"== {LAB}   dx {DX*1e6:.4f} um   index {sizes.size:,} boundary faces", flush=True)

    # patch 0 is fluid_to_solid, the conjugate interface, which is the only wall a channel has
    sel = np.flatnonzero((patch_of == 0) & (cent[:, 1] >= Y0) & (cent[:, 1] <= Y1))
    print(f"   patch 0 faces in the {(Y1-Y0)*1e6:.0f} um slab: {sel.size:,}", flush=True)
    if sel.size == 0:
        sys.exit("no faces in the slab: check y0/y1 against the part")

    offs = np.concatenate(([0], np.cumsum(sizes.astype(np.int64))))
    conn = rd("conn.u32", np.uint32)
    pts = rd("points.f32", np.float32).reshape(-1, 3)

    # three corners give the normal; the winding is the polyMesh one, so it points out of the owner
    # cell, which for a boundary face means out of this region
    o = offs[sel]
    p0 = pts[conn[o].astype(np.int64)].astype(np.float64)
    p1 = pts[conn[o + 1].astype(np.int64)].astype(np.float64)
    p2 = pts[conn[o + 2].astype(np.int64)].astype(np.float64)
    n = np.cross(p1 - p0, p2 - p0)
    ln = np.linalg.norm(n, axis=1)
    ok = ln > 0
    n[ok] /= ln[ok][:, None]
    keep = ok & (sizes[sel] == 4) & (np.abs(n[:, 0]) > 0.999)
    print(f"   of those, axis-aligned x-normal quads: {int(keep.sum()):,}"
          f"  (non-quad {int((sizes[sel] != 4).sum()):,},"
          f" off-axis {int((ok & (sizes[sel] == 4) & (np.abs(n[:, 0]) <= 0.999)).sum()):,})",
          flush=True)

    cx = cent[sel][keep, 0].astype(np.float64)
    ky = np.round(cent[sel][keep, 1].astype(np.float64) * 1e7).astype(np.int64)
    kz = np.round(cent[sel][keep, 2].astype(np.float64) * 1e7).astype(np.int64)
    sgn = np.sign(n[keep, 0])

    # pairing is only meaningful along one scan line, so sort into (y, z) rows first
    order = np.lexsort((cx, kz, ky))
    ky, kz, cx, sgn = ky[order], kz[order], cx[order], sgn[order]
    same = (ky[1:] == ky[:-1]) & (kz[1:] == kz[:-1])
    pair = same & (sgn[:-1] < 0) & (sgn[1:] > 0)
    g = cx[1:][pair] - cx[:-1][pair]
    g = g[g > 0]
    print(f"   scan lines {1 + int((~same).sum()):,}   gaps {g.size:,}", flush=True)
    return g * 1e6


def report(um, title):
    if um.size == 0:
        print(f"\n   {title}: nothing in band")
        return
    cells = um / (DX * 1e6)
    nr = np.round(cells).astype(np.int64)
    vals, cnt = np.unique(nr, return_counts=True)
    off = np.abs(cells - np.round(cells))
    w3 = um ** 3
    print(f"\n   {title}   n {um.size:,}")
    print(f"      width   median {np.median(um):9.3f} um   mean {um.mean():9.3f} um"
          f"   sd {um.std():8.3f} um   CV {100.0*um.std()/um.mean():6.2f}%")
    print(f"      cells   median {np.median(cells):8.4f}   mean {cells.mean():8.4f}"
          f"   off a whole number: median {np.median(off):.4f}, max {off.max():.4f}")
    print(f"      at the dominant count ({vals[cnt.argmax()]} cells): "
          f"{100.0*cnt.max()/um.size:.2f}%")
    for v, c in zip(vals, cnt):
        if c / um.size < 1e-4:
            continue
        print(f"         {v:5d} cells {np.median(um[nr == v]):10.3f} um {int(c):10,} "
              f"{100.0*c/um.size:6.2f}%")
    print(f"      w^3 flow spread p95/p5 {np.percentile(w3, 95)/np.percentile(w3, 5):.3f}x")
    big = um > 1000.0
    if big.any():
        # a handful of gaps cross the inlet or outlet plenum, tens of mm wide. They are real
        # geometry, not channels, and they are what makes the all-gaps sd and CV meaningless.
        print(f"      plus {int(big.sum()):,} gaps over 1 mm, up to {um.max()/1000:.1f} mm:"
              f" these cross the plenum and are what inflates the all-gaps sd")


um = gaps()
report(um, "ALL GAPS, array plus manifold")
if CORE:
    m = (um >= CORE[0]) & (um <= CORE[1])
    print(f"\n   core band {CORE[0]:.0f} to {CORE[1]:.0f} um holds {100.0*m.sum()/um.size:.2f}%"
          f" of gaps")
    report(um[m], f"MICROCHANNEL ARRAY ONLY, {CORE[0]:.0f} to {CORE[1]:.0f} um")
