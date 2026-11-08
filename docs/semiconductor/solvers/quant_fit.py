#!/usr/bin/env python3
"""Settle the sign and size of the quantisation flow bias, which two methods disagree about.

THE DISAGREEMENT
  The record says the mesh passes +10.1% too much coolant, from conductance (mean w^3 against a
  5-cell channel) of 1.1467 for the CAD and 1.2620 for the mesh.
  My forward model, raycasting the same fluid.stl and snapping to the lattice, said -1.3%.
  An independent reviewer reproduced the mesh column to five figures but got 1.13691 for the CAD
  column (+11.0%) and 1.12995 from the document's own bin means (+11.7%), i.e. the CAD column is
  the one that does not reproduce.

WHY IT MATTERS
  Flow bias is the product of a clean term (1 + 2/N^2, exact, verified) and this messy one. The
  messy one is the only one that redistributes flow BETWEEN branches, which is the one thing a
  topology-optimised plate cannot absorb. If it is +10% the mesh misleads; if it is -1% it does not.

THE TEST
  The mesh's own width histogram at dx = 20.1263 um was measured: 85.00% of channels on 5 cells,
  12.67% on 6, 2.33% on 4. That is three numbers my forward model must reproduce with ONE free
  parameter, the per-wall staircase bias b. If a single b fits all three, the model is right and
  its conductance answer is the one to believe.
"""
from __future__ import annotations

import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
STL = os.path.join(HERE, "mesh", "fluid.stl")
DX = 20.1263e-6
NOMINAL = 100.6315e-6
MEASURED = {4: 2.33, 5: 85.00, 6: 12.67}          # the mesh's own histogram, per cent


def load(path):
    raw = open(path, "rb").read()
    n = struct.unpack("<I", raw[80:84])[0]
    rec = np.frombuffer(raw, dtype=np.dtype([("n", "<3f4"), ("v", "<3,3f4"), ("a", "<u2")]),
                        count=n, offset=84)
    return rec["v"].astype(np.float64)


def gaps_along_x(tris, y0, z0):
    ay, az = tris[:, 0, 1], tris[:, 0, 2]
    by, bz = tris[:, 1, 1], tris[:, 1, 2]
    cy, cz = tris[:, 2, 1], tris[:, 2, 2]
    d = (by - ay) * (cz - az) - (bz - az) * (cy - ay)
    ok = np.abs(d) > 1e-20
    wa = (by - y0) * (cz - z0) - (bz - z0) * (cy - y0)
    wb = (cy - y0) * (az - z0) - (cz - z0) * (ay - y0)
    wc = (ay - y0) * (bz - z0) - (az - z0) * (by - y0)
    ins = ok & (((wa >= 0) & (wb >= 0) & (wc >= 0)) | ((wa <= 0) & (wb <= 0) & (wc <= 0)))
    if not ins.any():
        return np.empty(0)
    t, dd = tris[ins], d[ins]
    l0, l1, l2 = wa[ins] / dd, wb[ins] / dd, wc[ins] / dd
    x = np.sort(l0 * t[:, 0, 0] + l1 * t[:, 1, 0] + l2 * t[:, 2, 0])
    return np.empty(0) if x.size % 2 else x[1::2] - x[0::2]


def main():
    tris = load(STL)
    zmin, zmax = tris[:, :, 2].min(), tris[:, :, 2].max()
    ys = np.linspace(0.0220, 0.0280, 55)
    zs = np.linspace(zmin + 2e-4, zmax - 2e-4, 110)
    acc = []
    for y0 in ys:
        for z0 in zs:
            g = gaps_along_x(tris, y0, z0)
            if g.size:
                acc.append(g)
    w = np.concatenate(acc)
    ch = w[(w > 60e-6) & (w < 180e-6)]
    print("channel gaps sampled: %d" % ch.size)
    print("CAD p50 %.4f um (record 100.446), mean %.4f um, CV %.2f%%"
          % (np.percentile(ch, 50) * 1e6, ch.mean() * 1e6, 100 * ch.std() / ch.mean()))

    print("\n--- the CAD conductance column, computed directly ---")
    cad_cond = (ch ** 3).mean() / NOMINAL ** 3
    print("mean(w^3)/w_5cell^3 for the CAD population = %.5f" % cad_cond)
    print("   the record's CAD column        : 1.1467")
    print("   the reviewer's recomputation   : 1.13691")
    print("   the document's own bin means   : 1.12995")
    print("   this raycast                   : %.5f" % cad_cond)

    print("\n--- fit the per-wall staircase bias b to the mesh's measured histogram ---")
    print("%10s %9s %9s %9s %10s" % ("b um", "4 cells", "5 cells", "6 cells", "misfit"))
    best = None
    for b_um in np.arange(-3.0, 4.01, 0.05):
        seen = ch + 2.0 * b_um * 1e-6
        n = np.maximum(1, np.rint(seen / DX))
        sh = {k: 100.0 * (n == k).mean() for k in (4, 5, 6)}
        mis = sum((sh[k] - MEASURED[k]) ** 2 for k in MEASURED) ** 0.5
        if best is None or mis < best[0]:
            best = (mis, b_um, sh, n)
        if abs(b_um % 0.5) < 1e-9:
            print("%10.2f %9.2f %9.2f %9.2f %10.2f" % (b_um, sh[4], sh[5], sh[6], mis))
    mis, b_um, sh, n = best
    print("\nBEST FIT b = %+.3f um per wall, misfit %.2f points" % (b_um, mis))
    print("   predicted  4:%.2f%%  5:%.2f%%  6:%.2f%%" % (sh[4], sh[5], sh[6]))
    print("   measured   4:%.2f%%  5:%.2f%%  6:%.2f%%" % (MEASURED[4], MEASURED[5], MEASURED[6]))
    print("   the record asserts a bias of about +1.0 um per wall (+2 um on width)")

    wm = n * DX
    mesh_cond = (wm ** 3).mean() / NOMINAL ** 3
    ratio = (wm ** 3).mean() / (ch ** 3).mean()
    print("\n--- the answer ---")
    print("mesh conductance column   = %.5f   (record: 1.2620)" % mesh_cond)
    print("CAD  conductance column   = %.5f   (record: 1.1467)" % cad_cond)
    print("QUANTISATION FLOW BIAS    = %+.2f%%  (record: +10.1%%)" % (100 * (ratio - 1)))
    print("discretisation at N=5     = %+.2f%%  (exact, verified ratio 1.000000)" % (100 * 2 / 25))
    print("TOTAL                     = %+.2f%%" % (100 * (ratio * 1.08 - 1)))

    if mis > 6.0:
        print("\nNO single b fits the measured histogram, so the forward model is missing a")
        print("mechanism and NEITHER conductance number should be quoted. Say so.")
    else:
        print("\nOne b fits all three measured shares to %.2f points, so the forward model" % mis)
        print("reproduces the mesh that was actually built and its conductance answer is the")
        print("one to believe. The record's +10.1%% rests on a CAD column that three independent")
        print("recomputations put between 1.12995 and %.5f." % cad_cond)


if __name__ == "__main__":
    main()
