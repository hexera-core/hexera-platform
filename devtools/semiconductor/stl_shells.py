#!/usr/bin/env python3
"""How many closed shells does a binary STL carry, and is each one closed?

WHY THIS EXISTS. A mesh region that comes out in pieces is usually blamed on the mesher or on the
customer's CAD, and on this part it was once our own surface: a two-piece fluid.stl of ours split
the coolant into four, with 132 triangles missing. So before blaming anything, measure the surface.
On 6 November this script is what proved the opposite case, that cb_fluid.stl was one closed shell
and the coolant splitting into "three regions" was two stranded cells rather than a surface fault,
and that the chip really is two bodies and is supposed to mesh as two regions.

A shell is a connected component under shared vertices, quantised to 1 nm, which is 1/20000 of the
finest cell on this part, so coincident corners merge and nothing else does. A shell is closed when
every undirected edge in it is used exactly twice. An open shell is a hole, and a hole is what lets
a region leak.

The signed volume per shell is the number to cross-check against checkMesh's "Total volume" and
against the other regions: on this part the plate envelope minus the coolant matched the meshed
plate region to 0.065%, which is what proves the partition is right.

  usage: stl_shells.py <file.stl> [more.stl ...]
"""
import sys

import numpy as np

if len(sys.argv) < 2:
    sys.exit(__doc__)


def read_stl(path):
    b = np.fromfile(path, dtype=np.uint8)
    if b.size < 84:
        return None, 0, b.size
    n = int(np.frombuffer(b[80:84].tobytes(), dtype="<u4")[0])
    if 84 + 50 * n != b.size:
        return None, n, b.size          # ASCII, or truncated
    rec = b[84:].reshape(n, 50)
    tri = np.frombuffer(np.ascontiguousarray(rec[:, 12:48]).tobytes(), dtype="<f4")
    return tri.reshape(n, 3, 3).astype(np.float64), n, b.size


class UF:
    def __init__(self, n):
        self.p = np.arange(n)

    def find(self, x):
        p = self.p
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


for path in sys.argv[1:]:
    tri, n, nb = read_stl(path)
    name = path.rsplit("/", 1)[-1]
    if tri is None:
        print(f"{name:28s} not a binary STL ({n} declared, {nb} bytes)")
        continue

    v = np.round(tri.reshape(-1, 3) * 1e9).astype(np.int64)
    _, vid = np.unique(v, axis=0, return_inverse=True)
    vid = vid.reshape(n, 3)
    uf = UF(int(vid.max()) + 1)
    for a, b in ((0, 1), (1, 2), (2, 0)):
        for x, y in zip(vid[:, a], vid[:, b]):
            uf.union(int(x), int(y))
    roots = np.array([uf.find(int(x)) for x in vid[:, 0]])

    e = np.sort(np.stack([vid[:, [0, 1]], vid[:, [1, 2]], vid[:, [2, 0]]], 1).reshape(-1, 2), axis=1)
    _, ei, ec = np.unique(e, axis=0, return_inverse=True, return_counts=True)
    bad = (ec[ei].reshape(n, 3) != 2).any(axis=1)
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)

    shells, counts = np.unique(roots, return_counts=True)
    print(f"{name:28s} {n:9,} tri   {len(shells)} shell(s)   "
          f"open edges on {int(bad.sum()):,} tri   area {area.sum()*1e6:.3f} mm2")
    for s, c in sorted(zip(shells, counts), key=lambda t: -t[1]):
        m = roots == s
        vol = np.einsum("ij,ij->i", tri[m, 0], np.cross(tri[m, 1], tri[m, 2])).sum() / 6.0
        ob = int(bad[m].sum())
        print(f"    shell {c:9,} tri   vol {vol*1e9:13.4f} mm3   area {area[m].sum()*1e6:10.3f} mm2"
              f"   {'CLOSED' if ob == 0 else f'OPEN on {ob:,} tri'}")
