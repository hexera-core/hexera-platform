#!/usr/bin/env python3
"""How many disjoint bodies are in the chip STL, and how far apart are they?
If the STL is one body, a uniform 100.63 um lattice cut it in two. If it is two, the mesh is right."""
import struct
import sys
import io

PITCH = 100.6313e-6   # the chip's measured cell pitch


def read_stl(path):
    with open(path, "rb") as fh:
        head = fh.read(84)
        if head[:5] == b"solid" and b"facet" in head:
            fh.seek(0)
            txt = fh.read().decode("ascii", "replace")
            tris, cur = [], []
            for line in txt.splitlines():
                s = line.strip().split()
                if len(s) == 4 and s[0] == "vertex":
                    cur.append((float(s[1]), float(s[2]), float(s[3])))
                    if len(cur) == 3:
                        tris.append(tuple(cur)); cur = []
            return tris, "ascii"
        n = struct.unpack("<I", head[80:84])[0]
        tris = []
        for _ in range(n):
            d = fh.read(50)
            v = struct.unpack("<12fH", d)
            tris.append(((v[3], v[4], v[5]), (v[6], v[7], v[8]), (v[9], v[10], v[11])))
        return tris, "binary"


def shells(tris, snap=1e-9):
    key = lambda p: (round(p[0]/snap), round(p[1]/snap), round(p[2]/snap))
    ids, nxt = {}, 0
    tv = []
    for t in tris:
        a = []
        for p in t:
            k = key(p)
            if k not in ids:
                ids[k] = nxt; nxt += 1
            a.append(ids[k])
        tv.append(a)
    par = list(range(nxt))
    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]; x = par[x]
        return x
    def uni(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: par[ra] = rb
    for a in tv:
        uni(a[0], a[1]); uni(a[1], a[2])
    groups = {}
    for i, a in enumerate(tv):
        groups.setdefault(find(a[0]), []).append(i)
    return groups, nxt


for path in sys.argv[1:]:
    tris, fmt = read_stl(path)
    groups, npts = shells(tris)
    print("%s  %s  %d triangles  %d unique points  %d disjoint bodies"
          % (path.split("/")[-1], fmt, len(tris), npts, len(groups)))
    bbs = []
    for g, idx in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        xs = [p[0] for i in idx for p in tris[i]]
        ys = [p[1] for i in idx for p in tris[i]]
        zs = [p[2] for i in idx for p in tris[i]]
        # signed volume by the divergence theorem
        V = 0.0
        for i in idx:
            a, b, c = tris[i]
            V += (a[0]*(b[1]*c[2]-b[2]*c[1]) - a[1]*(b[0]*c[2]-b[2]*c[0])
                  + a[2]*(b[0]*c[1]-b[1]*c[0])) / 6.0
        bbs.append((min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
        print("   body %2d: %4d tris  vol %+.4e m3 = %9.3f mm3   "
              "x [%+.5f %+.5f] y [%+.5f %+.5f] z [%+.5f %+.5f]"
              % (len(bbs), len(idx), V, abs(V)*1e9,
                 min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)))
        print("            size %7.3f x %7.3f x %7.3f mm = %6.1f x %6.1f x %6.1f cells"
              % ((max(xs)-min(xs))*1e3, (max(ys)-min(ys))*1e3, (max(zs)-min(zs))*1e3,
                 (max(xs)-min(xs))/PITCH, (max(ys)-min(ys))/PITCH, (max(zs)-min(zs))/PITCH))
    if len(bbs) > 1:
        print("   gaps between body bounding boxes, in cells of %.3f um:" % (PITCH*1e6))
        for i in range(len(bbs)):
            for j in range(i+1, len(bbs)):
                g = []
                for k, lab in ((0, "x"), (2, "y"), (4, "z")):
                    lo1, hi1 = bbs[i][k], bbs[i][k+1]
                    lo2, hi2 = bbs[j][k], bbs[j][k+1]
                    if hi1 < lo2:   d = lo2 - hi1
                    elif hi2 < lo1: d = lo1 - hi2
                    else:           d = 0.0
                    g.append("%s %.3f mm (%.2f cells)" % (lab, d*1e3, d/PITCH))
                print("      body %d to body %d: %s" % (i+1, j+1, "  ".join(g)))
    print()
