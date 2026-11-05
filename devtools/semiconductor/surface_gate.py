"""Refuse to ship a surface that cannot do its job. Run before any push to a mesh box.

THE DEFECT THIS CATCHES, measured 2026-11-05. The coolant came out of a 326M-cell mesh as four
disconnected pieces and I was about to ask Corintis whether their plate has a second coolant
volume. It does not. The `fluid.stl` we handed cfMesh was already TWO surface pieces: the second is
1,596 triangles and 1.4968e-06 m3, and the mesh's 134,234-cell island is 1.4979e-06 m3, a 0.07%
match. 132 triangles had gone missing in a re-export and that severed the neck into a plenum.
`parts.py` has had `components`, `volume_m3` and `passages` the whole time and nothing called them
before a mesh started, so the mesher spent 1,013 s and the defect surfaced as a question for the
customer.

THE RULES ARE PER ROLE, not one rule for every file. A first version of this applied "one closed
piece" to everything and refused two surfaces that are correct:

  chip_fit.stl came back as 2 pieces. The chip IS two disjoint solids - 68 triangles at
  3.307595e-06 m3 and 28 at 1.298726e-05 m3, summing to the 1.629485e-05 m3 the fused CAD gives -
  and cbox2's chip region duly checks out as 2 regions with the right volume. Two shells is the
  geometry, so the gate has to be told to expect two.

  fluid_fine.stl came back with 111 edges not shared by exactly two triangles and one orphan. It
  is a REFINEMENT surface only, never a topoSet source: `surfaceMeshRefinement` refines cells near
  triangles and needs no inside. So openness is reported and not refused.

  role            closed  pieces  why
  topo            yes     stated  a surfaceToCell source. useSurfaceOrientation believes the
                                  normals, so the signed volume must be positive, and an extra
                                  piece becomes an island region that splitMeshRegions will not
                                  flag, because an island IS a region.
  domain          yes     1       cfMesh meshes exactly one shell
  refine          no      any     reported, never refused

usage: surface_gate.py <role>:<pieces>:<file.stl> [...]
       surface_gate.py --coupled <dir>        the five surfaces a coupled run needs
exit 0 if every surface can do its job, 1 otherwise.
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/home/rehaan/hexera-platform-semi/devtools/semiconductor")
import parts as P  # noqa: E402

WELD = 1e-9
#: what a coupled run hands cfMesh, and what each one has to be
COUPLED = [("domain", 1, "domain_box.stl"),
           ("topo", 1, "block.stl"),
           ("topo", 1, "fluid.stl"),
           ("topo", 2, "chip_fit.stl"),
           ("refine", 0, "fluid_fine.stl")]


def _key(v):
    return (round(v[0] / WELD), round(v[1] / WELD), round(v[2] / WELD))


def edge_counts(tris):
    c = Counter()
    for t in tris:
        k = [_key(v) for v in t]
        for a, b in ((k[0], k[1]), (k[1], k[2]), (k[2], k[0])):
            c[(a, b) if a <= b else (b, a)] += 1
    return c


def orphans(tris, counts):
    out = []
    for i, t in enumerate(tris):
        k = [_key(v) for v in t]
        if all(counts[(a, b) if a <= b else (b, a)] < 2
               for a, b in ((k[0], k[1]), (k[1], k[2]), (k[2], k[0]))):
            out.append(i)
    return out


def check(role, want_pieces, path):
    path = Path(path)
    if not path.is_file():
        print("  %-18s MISSING" % path.name)
        return False
    tris = P.read_stl(path)
    comps = P.components(tris)
    vol = P.volume_m3(tris)
    lo, hi = P.bounds(tris)
    counts = edge_counts(tris)
    odd = sum(1 for n in counts.values() if n != 2)
    orph = orphans(tris, counts)
    print("  %-7s %-18s %9s tri  %d piece(s)  vol %.6e m3  %s mm"
          % (role, path.name, f"{len(tris):,}", len(comps), vol,
             " x ".join("%.3f" % ((hi[i] - lo[i]) * 1e3) for i in range(3))))
    bad = []
    if role in ("topo", "domain"):
        if odd:
            bad.append("%s edges not shared by exactly two triangles, so it is not closed and "
                       "has no inside for surfaceToCell to find" % f"{odd:,}")
        if vol <= 0:
            bad.append("signed volume %.6e is not positive, so the normals point inward" % vol)
        if orph:
            bad.append("%d orphan triangle(s), ids %s" % (len(orph), orph[:6]))
        expect = want_pieces or 1
        if len(comps) != expect:
            bad.append("%d pieces, expected %d" % (len(comps), expect))
            for j, c in enumerate(sorted(comps, key=len, reverse=True)[:6]):
                sub = [tris[i] for i in c]
                print("       piece %d: %s tri, %.6e m3"
                      % (j, f"{len(sub):,}", P.volume_m3(sub)))
    else:
        note = []
        if odd:
            note.append("%s open or non-manifold edges" % f"{odd:,}")
        if orph:
            note.append("%d orphan triangle(s)" % len(orph))
        print("     refinement only, so topology is reported and not refused%s"
              % (": " + ", ".join(note) if note else ""))
    for b in bad:
        print("     REFUSED: %s" % b)
    if not bad and role in ("topo", "domain"):
        print("     ok: closed, positive volume, %d piece(s) as expected" % len(comps))
    return not bad


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--coupled":
        d = Path(args[1] if len(args) > 1 else "/home/rehaan/clean_stl")
        jobs = [(r, n, d / f) for r, n, f in COUPLED]
    elif args:
        jobs = []
        for a in args:
            role, n, f = a.split(":", 2)
            jobs.append((role, int(n or 0), f))
    else:
        raise SystemExit(__doc__.split("usage:")[1])
    print("=== surface gate, %d file(s)" % len(jobs))
    res = [check(r, n, f) for r, n, f in jobs]
    print()
    print("GATE PASSED" if all(res) else
          "GATE FAILED, %d of %d refused" % (res.count(False), len(res)))
    sys.exit(0 if all(res) else 1)
