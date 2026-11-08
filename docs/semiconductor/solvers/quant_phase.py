#!/usr/bin/env python3
"""The correct quantisation model: each WALL snaps to a cell face independently.

WHY THE LAST MODEL FAILED. It applied one global bias b to the width and then rounded the width.
That cannot produce a channel narrower than anything in the CAD, so it predicted 0.00% of channels
on 4 cells where the mesh measured 2.33%. The record notes the same fact from the other side: the
mesh puts 1.85% of channels at 80.5 um while the narrowest run the part contains is 97.963 um.

WHAT ACTUALLY HAPPENS. splitMeshRegions cuts along cell faces, so the LEFT wall and the RIGHT wall
each land on the nearest face independently. With the wall phase inside a cell measured as uniform
(20 bins, counts 290-482 about a mean of 370), the cell count across a channel is

    n = round((p + w)/dx) - round(p/dx),   p uniform on [0, dx)

which for w near but not exactly a whole number of cells gives a spread over n-1, n, n+1 all by
itself. There is NO free parameter here. So reproducing the measured 2.33 / 85.00 / 12.67 split is
a genuine test of the mechanism rather than a fit.
"""
from __future__ import annotations

import os
import struct

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
STL = os.path.join(HERE, "mesh", "fluid.stl")
PITCH = 201.26253e-6
NOMINAL = 100.6315e-6
MEASURED = {4: 2.33, 5: 85.00, 6: 12.67}
NPHASE = 64                                   # phase draws per channel


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


def snap_counts(ch, dx, offset=0.0):
    """Cell counts across each channel, averaged over the uniform wall phase. No free parameter."""
    w = ch - 2.0 * offset
    ph = (np.arange(NPHASE) + 0.5) / NPHASE * dx          # deterministic uniform sweep
    n = np.rint((w[:, None] + ph[None, :]) / dx) - np.rint(ph[None, :] / dx)
    return np.maximum(1.0, n)                              # (nchan, NPHASE)


def main():
    tris = load(STL)
    zmin, zmax = tris[:, :, 2].min(), tris[:, :, 2].max()
    acc = []
    for y0 in np.linspace(0.0220, 0.0280, 55):
        for z0 in np.linspace(zmin + 2e-4, zmax - 2e-4, 110):
            g = gaps_along_x(tris, y0, z0)
            if g.size:
                acc.append(g)
    w = np.concatenate(acc)
    ch = w[(w > 60e-6) & (w < 180e-6)]
    print("channel gaps: %d, CAD p50 %.4f um, mean %.4f um"
          % (ch.size, np.percentile(ch, 50) * 1e6, ch.mean() * 1e6))

    DX = PITCH / 10                                        # 20.1263 um, what was built
    n = snap_counts(ch, DX)
    sh = {k: 100.0 * (n == k).mean() for k in (4, 5, 6)}
    print("\n--- the test: no free parameter, against the mesh's own histogram ---")
    print("%-12s %8s %8s %8s" % ("", "4 cells", "5 cells", "6 cells"))
    print("%-12s %8.2f %8.2f %8.2f" % ("predicted", sh[4], sh[5], sh[6]))
    print("%-12s %8.2f %8.2f %8.2f" % ("measured", MEASURED[4], MEASURED[5], MEASURED[6]))
    mis = sum((sh[k] - MEASURED[k]) ** 2 for k in MEASURED) ** 0.5
    print("misfit %.2f points, with nothing fitted" % mis)

    w3_cad = (ch ** 3).mean()

    def bias(dx, offset=0.0):
        nn = snap_counts(ch, dx, offset)
        wm = nn * dx
        return (wm ** 3).mean() / w3_cad, 100.0 * (nn == round(NOMINAL / dx)).mean()

    q, onnom = bias(DX)
    disc = 1.0 + 2.0 / 25.0
    print("\n--- the answer at the built lattice, dx 20.1263 um ---")
    print("on nominal (5 cells)      %.2f%%   (measured 85.00%%)" % onnom)
    print("QUANTISATION FLOW BIAS    %+.2f%%   (the record says +10.1%%)" % (100 * (q - 1)))
    print("discretisation at N=5     %+.2f%%   (exact, verified)" % (100 * (disc - 1)))
    print("TOTAL too much coolant    %+.2f%%" % (100 * (q * disc - 1)))

    print("\n--- now the lattice choice, with the mechanism modelled correctly ---")
    print("%-7s %9s %4s %10s %11s %11s %11s"
          % ("pitch", "dx um", "N", "on-nom %", "quant", "disc", "TOTAL"))
    for k in (8, 10):
        dx = PITCH / k
        q2, on2 = bias(dx)
        d2 = 1.0 + 2.0 / (round(NOMINAL / dx) ** 2)
        print("%-7d %9.4f %4d %10.2f %+10.2f%% %+10.2f%% %+10.2f%%"
              % (k, dx * 1e6, round(NOMINAL / dx), on2, 100 * (q2 - 1),
                 100 * (d2 - 1), 100 * (q2 * d2 - 1)))

    print("\n--- the free inward offset, re-tested against the real mechanism ---")
    print("%-7s %9s %10s %11s %11s" % ("pitch", "offset um", "on-nom %", "quant", "TOTAL"))
    for k in (8, 10):
        dx = PITCH / k
        d2 = 1.0 + 2.0 / (round(NOMINAL / dx) ** 2)
        for o in (0.0, 0.5, 1.0, 1.5, 2.0):
            q2, on2 = bias(dx, o * 1e-6)
            print("%-7d %9.2f %10.2f %+10.2f%% %+10.2f%%"
                  % (k, o, on2, 100 * (q2 - 1), 100 * (q2 * d2 - 1)))
        print()


if __name__ == "__main__":
    main()
