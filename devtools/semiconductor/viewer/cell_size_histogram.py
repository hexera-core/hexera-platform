#!/usr/bin/env python3
"""The cell-size distribution of the delivered mesh, by volume, per region.

cbrt(cellVolume) is the cell's edge length. An octree can only halve, so a graded mesh lands in
bins a factor of 2 apart and a uniform one lands in a single bin. The anisotropic stretch
(scaleY 3) means a "cell" is not a cube, so cbrt is an equivalent edge, not a side - which is the
right thing to compare against the background cell anyway since that is how cell COUNT scales.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "/opt/mp/src")
from meshpipeline.render.boundary_quality import _internal_field  # noqa: E402

CASE = Path("/mnt/work/cbox8")
BG = 25.1578e-6
NPROC = 8

TOTAL = {"fluid": 55_303_441, "solid": 199_394_482, "chip": 15_726_384}

grand = []
for region in ("fluid", "solid", "chip"):
    vals = []
    for p in range(NPROC):
        f = CASE / ("processor%d" % p) / "constant" / region / "cellVolume"
        if not f.is_file():
            continue
        uni, cells = _internal_field(f)
        if cells is None:
            continue
        vals.append(cells.astype(np.float64))
    if not vals:
        print("%-6s no cellVolume written" % region)
        continue
    v = np.concatenate(vals)
    edge = np.cbrt(v) * 1e6  # um
    grand.append(edge)
    n = edge.size
    ok = "OK" if n == TOTAL[region] else "*** %d vs %d" % (n, TOTAL[region])
    print("=== %-6s %s cells  %s ===" % (region, f"{n:,}", ok))
    print("     equivalent edge um: min %8.3f  median %8.3f  mean %8.3f  max %9.3f"
          % (edge.min(), np.median(edge), edge.mean(), edge.max()))
    ratio = edge / (BG * 1e6)
    bands = [(0, 0.75, "finer than background"), (0.75, 1.5, "1x  25.16 um"),
             (1.5, 3.0, "2x  50.32 um"), (3.0, 6.0, "4x  100.63 um"),
             (6.0, 12.0, "8x  201.26 um"), (12.0, 1e9, "16x+ 402.5 um and coarser")]
    for lo, hi, lbl in bands:
        m = (ratio >= lo) & (ratio < hi)
        c = int(m.sum())
        if c:
            print("       %-28s %14s  %5.1f%%  (volume share %5.1f%%)"
                  % (lbl, f"{c:,}", 100.0 * c / n, 100.0 * v[m].sum() / v.sum()))
    print()

if grand:
    e = np.concatenate(grand)
    print("=== all %s delivered cells ===" % f"{e.size:,}")
    print("    equivalent edge um: min %.3f  median %.3f  max %.3f"
          % (e.min(), np.median(e), e.max()))
    fine = (e / (BG * 1e6)) < 1.5
    print("    at or below 1x background : %s cells (%.1f%%)"
          % (f"{int(fine.sum()):,}", 100.0 * fine.mean()))
    print()
    # What a UNIFORM mesh at the finest delivered size would have cost, which is the
    # efficiency number the grading buys.
    finest = e.min() * 1e-6
    # domain extent from the delivered cells' own total volume
    print("    finest delivered cell      : %.3f um" % (e.min()))
    print("    if every cell were that size, the same volume would need:")
    tot_vol = (np.concatenate([np.cbrt(x) ** 3 for x in [e]]) * 1e-18).sum()
    print("      delivered volume %.6e m3  ->  %s cells"
          % (tot_vol, f"{int(tot_vol / finest ** 3):,}"))
