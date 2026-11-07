#!/usr/bin/env python3
"""
ARE BOTH PORTS IN THE SAME CONNECTED PIECE OF COOLANT?

checkMesh says the coolant region is in 3 pieces. That is fine if and only if the two ports are in
the SAME piece and the other pieces are negligible: an inlet and an outlet in different circuits is
a mesh that cannot carry flow at all, and it would pass every geometric check in checkMesh.

This was verified for cbox2b. cbox2g moved 23,281 cells across the conjugate interface, so the
stray pieces are not guaranteed to be the same ones, and the port split has just rewritten the
patches. So verify it again, on the decomposed mesh, by walking cell-to-cell connectivity through
owner/neighbour and then asking which component each port face's owner cell is in.

Reads the decomposed mesh directly; processor boundaries are stitched by matching each
processorNNtoMM patch with its twin, which is how OpenFOAM pairs them (face i of one is face i of
the other).
"""
import os
import re
import struct
import sys
import collections

CASE = sys.argv[1] if len(sys.argv) > 1 else "/mnt/work/cbox2g"
REGION = sys.argv[2] if len(sys.argv) > 2 else "fluid"


def read_foam_list(path):
    """Return a list of ints from an OpenFOAM labelList, ascii or binary."""
    with open(path, "rb") as f:
        raw = f.read()
    # find the end of the header
    i = raw.find(b"// * * *")
    i = raw.find(b"\n", i) + 1 if i >= 0 else 0
    rest = raw[i:]
    m = re.search(rb"(\d+)\s*\(", rest)
    if not m:
        return []
    n = int(m.group(1))
    start = m.end()
    fmt = b"binary" if b"format" in raw[:i] and b"binary" in raw[:i] else b"ascii"
    if fmt == b"binary":
        # labels are 32-bit in this build
        buf = rest[start:start + 4 * n]
        return list(struct.unpack("<%di" % n, buf))
    # ascii
    end = rest.find(b")", start)
    return [int(x) for x in rest[start:end].split()]


def read_boundary(path):
    txt = open(path, errors="replace").read()
    i = txt.find("// * * *")
    body = txt[txt.find("\n", i) + 1:] if i >= 0 else txt
    out = []
    for m in re.finditer(r"(\w+)\s*\{([^}]*)\}", body):
        name, b = m.group(1), m.group(2)
        nf = re.search(r"nFaces\s+(\d+)", b)
        sf = re.search(r"startFace\s+(\d+)", b)
        ty = re.search(r"type\s+(\w+)", b)
        nb = re.search(r"neighbProcNo\s+(\d+)", b)
        mp = re.search(r"myProcNo\s+(\d+)", b)
        if nf and sf:
            out.append(dict(name=name, nFaces=int(nf.group(1)), startFace=int(sf.group(1)),
                            type=ty.group(1) if ty else "?",
                            neighb=int(nb.group(1)) if nb else None,
                            mine=int(mp.group(1)) if mp else None))
    return out


procs = sorted([d for d in os.listdir(CASE) if re.fullmatch(r"processor\d+", d)],
               key=lambda s: int(s[9:]))
print("processors: %d" % len(procs))

owner, neigh, bnd, ncells = {}, {}, {}, {}
offset, tot = {}, 0
for p in procs:
    base = os.path.join(CASE, p, "constant", REGION, "polyMesh")
    owner[p] = read_foam_list(os.path.join(base, "owner"))
    neigh[p] = read_foam_list(os.path.join(base, "neighbour"))
    bnd[p] = read_boundary(os.path.join(base, "boundary"))
    ncells[p] = max(max(owner[p]), max(neigh[p]) if neigh[p] else 0) + 1
    offset[p] = tot
    tot += ncells[p]
print("cells: %d" % tot)

# union-find over global cell ids
parent = list(range(tot))


def find(a):
    while parent[a] != a:
        parent[a] = parent[parent[a]]
        a = parent[a]
    return a


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[max(ra, rb)] = min(ra, rb)


for p in procs:
    off = offset[p]
    o, n = owner[p], neigh[p]
    for i in range(len(n)):
        union(off + o[i], off + n[i])

# stitch processor boundaries: patch processorAtoB on A pairs face-for-face with processorBtoA on B
pairs = 0
for p in procs:
    for pa in bnd[p]:
        if pa["type"] != "processor":
            continue
        a, b = pa["mine"], pa["neighb"]
        if a is None or b is None or a > b:
            continue
        q = "processor%d" % b
        twin = None
        for pb in bnd[q]:
            if pb["type"] == "processor" and pb["mine"] == b and pb["neighb"] == a:
                twin = pb
                break
        if twin is None or twin["nFaces"] != pa["nFaces"]:
            print("  ** could not pair %s on %s" % (pa["name"], p))
            continue
        for k in range(pa["nFaces"]):
            union(offset[p] + owner[p][pa["startFace"] + k],
                  offset[q] + owner[q][twin["startFace"] + k])
        pairs += pa["nFaces"]
print("processor faces stitched: %d" % pairs)

comp = collections.Counter(find(c) for c in range(tot))
print()
print("connected pieces: %d" % len(comp))
for root, n in comp.most_common(10):
    print("   piece root %-12d %12d cells   %.6f%%" % (root, n, 100.0 * n / tot))

print()
print("WHICH PIECE IS EACH PORT IN?")
for want in ("port_a", "port_b", "fluid_to_air"):
    roots = collections.Counter()
    nf = 0
    for p in procs:
        for pa in bnd[p]:
            if pa["name"] != want:
                continue
            nf += pa["nFaces"]
            for k in range(pa["nFaces"]):
                roots[find(offset[p] + owner[p][pa["startFace"] + k])] += 1
    if nf == 0:
        continue
    print("   %-14s %6d faces" % (want, nf))
    for r, c in roots.most_common(5):
        print("        piece root %-12d %6d faces   (that piece has %d cells)" % (r, c, comp[r]))

ra = set()
rb = set()
for p in procs:
    for pa in bnd[p]:
        if pa["name"] in ("port_a", "port_b"):
            s = ra if pa["name"] == "port_a" else rb
            for k in range(pa["nFaces"]):
                s.add(find(offset[p] + owner[p][pa["startFace"] + k]))
print()
if ra and rb:
    if ra == rb and len(ra) == 1:
        big = comp.most_common(1)[0][0]
        print("VERDICT: both ports are in ONE piece, root %d, which is %s" %
              (list(ra)[0], "THE MAIN PIECE" if list(ra)[0] == big else "*** NOT the main piece"))
        print("         So the coolant is a single circuit from inlet to outlet. Usable.")
    else:
        print("*** VERDICT: the ports are NOT in a single shared piece. port_a roots %s, port_b %s"
              % (sorted(ra), sorted(rb)))
        print("*** This mesh cannot carry flow. It would still pass every checkMesh criterion.")
else:
    print("ports not found as separate patches yet")
