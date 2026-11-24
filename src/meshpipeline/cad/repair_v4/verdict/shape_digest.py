# Responsibility: An exact content digest of an OCCT shape, and a small per-worker LRU keyed by it, so a kernel
#                 result that is a pure function of one shape is computed once per worker.
# Owns: `digest`, `geometry_key` (the round-trip-stable key, lane C16), `WorkerMemo`.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. It never decides anything:
#             callers memoise only results that depend on nothing but the digested shape.
"""Why the digest is exact: it is the sha256 of the shape's `BinTools` serialisation, which writes geometry,
topology, locations, orientations, tolerances and flags with raw IEEE doubles (a 1-ulp change to one coordinate
changes it; tested natively). Two shapes with the same digest are the same input to any OCCT algorithm, so a
deterministic result computed on one is the result on the other.

It is NOT a similarity measure: a STEP round trip never reproduces a shape bit for bit (Lane A, 60/60 files), so the
digest never claims the round-tripped shape equals the input. It pays off where shapes really are identical: the
untouched bodies of a search candidate, and states the search reaches more than once.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

_DIR: list[str] = []


def digest(shape: Any) -> str:
    """sha256 of `shape`'s BinTools serialisation, triangulation and normals included: bounding boxes (and so the
    Booleans) read a triangulation when one is present, so it is part of the input."""
    from OCP.BinTools import BinTools, BinTools_FormatVersion

    if not _DIR:
        _DIR.append(tempfile.mkdtemp(prefix="shape_digest_"))
    path = os.path.join(_DIR[0], f"{os.getpid()}.bin")
    if not BinTools.Write_s(shape, path, True, True, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT):
        raise RuntimeError("BinTools could not serialise the shape")
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


#: lane C16: the resolution `geometry_key` rounds to, as a fraction of the shape's own optimal-box diagonal (lengths),
#: its square (areas) and its cube (volumes). Far below any real edit, far above a STEP round trip's float noise.
GEOMETRY_KEY_RESOLUTION = 1e-9


def geometry_key(shape: Any) -> str:
    """A key that is STABLE across a STEP round trip and changes with any real edit - THE one shared function for
    memoising a result that depends only on a shape's geometry (lane C16; Lanes A and B build their shape memos on it).

    `digest` is exact but not stable: on dev a2807f9a2fef all 3 unchanged solids got new BinTools digests after one
    `step_for` export + re-read, and 2 of 3 changed again on a second, so a digest memo never hits across judgements.
    This key hashes what an export cannot change but an edit must: per solid, its SIGNED volume and centre of
    mass; per face, its surface type, area and centroid (a sorted multiset, so face order does not matter); and the
    counts of solids, faces, edges and vertices. Every real number is rounded to GEOMETRY_KEY_RESOLUTION of the shape's
    own optimal-box diagonal (to its power for areas and volumes). Two shapes with the same key agree on all of that to
    1e-9 of their size; a false hit would need two different solids identical in every face's type, area and centroid
    at that resolution. A miss (a value straddling a rounding edge) only costs a recomputation.

    It does NOT carry tolerances or orientation flags. Measured on v4's stored states (C16): per solid it survives one
    round trip 290/311 times (the digest 109/311); of 48 parent->child edits that changed the digest, 33 kept the key,
    all `orient_and_close` with identical max tolerances and reversed-face counts, and layer 1's full `check_shape`
    gave the IDENTICAL result on both sides of all 33. A memo whose result reads tolerances must add them itself.

    COINCIDENT DUPLICATE BODIES SHARE A KEY, BY DESIGN, and a memo keyed on this must expect it. The key carries each
    solid's centre of mass, so two solids with one key are the same geometry in the same place - a duplicate body.
    Measured on Lane C's 1,391-state corpus (A17): on the worst state 11 solids give 8 distinct keys, and its 55
    candidate solid PAIRS collapse to 29 keys, so `valid_kernel.OVERLAP_PAIRS` serves one pair's stored decision to
    its twin. That is correct for the DECISION - a pair of coincident twins is the same geometric question - and it
    makes a `body_overlap` finding's `detail.volume` / `detail.thickness` NOT BIT-REPRODUCIBLE across a memo
    boundary: they come from whichever twin pair was computed first. Measured drift on 12 of 1,391 states, all
    within 1e-9 relative, with every code, entity list and finding count identical.

    THAT IS SAFE ONLY WHILE NOTHING DECIDES ON THOSE MAGNITUDES. It is not that nothing reads a magnitude - B24's
    `search_seq` derives parameters from a defect's measurement - but it reads the SITE GRAPH's numbers (`sites`:
    a node's area and min_width, a clash's dist_mm, diagonal_mm), not a `check_shape` finding's detail. IF ANY
    FUTURE CODE MAKES A DECISION, GATE, SCORE OR OP PARAMETER OUT OF `detail.volume` OR `detail.thickness`, THE
    PAIR MEMO MUST BE REVISITED (LEAD-3's condition, 2026-11-24)."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        return hashlib.sha256(b"void").hexdigest()
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = max(((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5, 1e-12)
    q = GEOMETRY_KEY_RESOLUTION

    def r(v: float, power: int = 1) -> int:
        return round(v / (q * diag ** power))

    def mapped(kind: Any) -> TopTools_IndexedMapOfShape:
        m = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, kind, m)
        return m

    solids, faces = mapped(TopAbs_SOLID), mapped(TopAbs_FACE)
    rows: list[tuple] = [("counts", solids.Extent(), faces.Extent(), mapped(TopAbs_EDGE).Extent(),
                          mapped(TopAbs_VERTEX).Extent())]
    for i in range(1, solids.Extent() + 1):
        g = GProp_GProps()
        BRepGProp.VolumeProperties_s(solids.FindKey(i), g)
        c = g.CentreOfMass()
        rows.append(("solid", r(g.Mass(), 3), r(c.X()), r(c.Y()), r(c.Z())))
    face_rows = []
    for i in range(1, faces.Extent() + 1):
        f = TopoDS.Face_s(faces.FindKey(i))
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        c = g.CentreOfMass()
        face_rows.append((int(BRepAdaptor_Surface(f).GetType()), r(g.Mass(), 2), r(c.X()), r(c.Y()), r(c.Z())))
    rows.extend(sorted(face_rows))
    rows.sort(key=repr)
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def file_digest(path: str) -> str:
    """sha256 of a file's bytes: a BREP file IS the shape it is read into."""
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


class WorkerMemo:
    """A bounded LRU for one worker process. Only a computed value is stored; a raise stores nothing."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._items: OrderedDict[Hashable, Any] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get_or_compute(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        if key in self._items:
            self._items.move_to_end(key)
            self.hits += 1
            return self._items[key]
        self.misses += 1
        value = compute()
        self._items[key] = value
        while len(self._items) > self._size:
            self._items.popitem(last=False)
        return value
