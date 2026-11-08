#!/usr/bin/env python3
"""Split the bad faces by the surface they were extruded off, coarse wall or channel wall."""
import io
from collections import Counter

CSV = "/home/rehaan/qa/badfaces.csv"
P = 25.1578e-6                 # level +4
FINE_A = P * (3 * P)           # 25.158 x 75.474 um, a stretched level-4 face
COARSE_A = (4 * P) * (12 * P)  # 100.631 x 301.894 um, a stretched level-2 face
CZ, LZ = 0.0424025383, 0.05133793399
BZ = (CZ - LZ / 2, CZ + LZ / 2)

rows = []
with io.open(CSV) as fh:
    fh.readline()
    for line in fh:
        p = line.rstrip().split(",")
        rows.append(dict(a=float(p[1]), x=float(p[2]), y=float(p[3]), z=float(p[4]),
                         vo=float(p[5]), vn=float(p[6]), A=float(p[7])))
n = len(rows)
print("a stretched level-4 face is %.4e m2 (side %.1f um)" % (FINE_A, FINE_A ** .5 * 1e6))
print("a stretched level-2 face is %.4e m2 (side %.1f um)" % (COARSE_A, COARSE_A ** .5 * 1e6))
print()

groups = {"channel wall, level +4": [], "coarse wall, level +2": [], "other": []}
for r in rows:
    if 0.6 * FINE_A <= r["A"] <= 1.8 * FINE_A:
        groups["channel wall, level +4"].append(r)
    elif 0.6 * COARSE_A <= r["A"] <= 1.8 * COARSE_A:
        groups["coarse wall, level +2"].append(r)
    else:
        groups["other"].append(r)

print("%-24s %6s %8s %9s %9s %11s %9s" %
      ("group", "faces", "share", "angle min", "angle max", "thickness", "nearbox"))
for k, g in groups.items():
    if not g:
        print("%-24s %6d" % (k, 0))
        continue
    th = sorted(min(r["vo"], r["vn"]) / r["A"] * 1e6 for r in g)
    nb = sum(1 for r in g
             if min(abs(r["z"] - BZ[0]), abs(r["z"] - BZ[1])) < 1.2e-3)
    print("%-24s %6d %7.2f%% %9.3f %9.3f %8.2f um %8.1f%%" %
          (k, len(g), 100.0 * len(g) / n, min(r["a"] for r in g),
           max(r["a"] for r in g), th[len(th) // 2], 100.0 * nb / len(g)))
print()

print("thickness of the thinner cell, 0.25 um bins, whole set")
th = Counter(round(min(r["vo"], r["vn"]) / r["A"] * 4e6) / 4.0 for r in rows)
for t, c in th.most_common(8):
    print("   %6.2f um  %5d  %6.2f%%" % (t, c, 100.0 * c / n))
print()

print("neighbour volume ratio by group")
for k, g in groups.items():
    if not g:
        continue
    vr = sorted(max(r["vo"], r["vn"]) / min(r["vo"], r["vn"]) for r in g)
    print("   %-24s median %.2f  p10 %.2f  p90 %.2f"
          % (k, vr[len(vr) // 2], vr[int(.1 * len(vr))], vr[int(.9 * len(vr))]))
print()

print("the tangential drift the angle implies, on the thinner cell")
import math
for k, g in groups.items():
    if not g:
        continue
    d = sorted(min(r["vo"], r["vn"]) / r["A"] * math.tan(math.radians(r["a"])) * 1e6
               for r in g)
    print("   %-24s median %.2f um  max %.2f um" % (k, d[len(d) // 2], d[-1]))
