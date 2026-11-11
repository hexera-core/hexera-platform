# Responsibility: THE SAFE PART OF GEOMETRY CLEAN-UP FOR A SURFACE FILE (SURF-8..10): what a pre-processor user does by
# hand before meshing a surface model - stitch small gaps, leave out a face drawn twice, fill a pinhole - done by
# rule, the same way every time, and reported: a face written twice is left out once, the gaps between
# neighbouring faces are closed rung by rung up a measured sewing ladder (each rung judged before it is kept), a hole
# smaller than the gaps being closed is filled with the face's own surface, and whatever is left open is measured
# and placed.
# Boundaries: geometry only, deterministic, no model call. It never invents geometry: no face is added, moved or
# reshaped; only edges that lie along each other are made one, a duplicate left out, a pinhole's edge taken off
# its face. Two edges of ONE face are never sewn to each other (a slot or slit drawn in a face is a feature). It
# reads what kind of file this is from no one: cad/surface_model decides that first, on the file as written, and
# calls this only for a surface part. cad/repair is never called or edited.
# Collaborates with: cad/surface_model.py (calls it, measures the result), cad/midsurface.py (small face helpers),
# contracts/surface_stitch.py (a tolerance the user's notes state), engines/surface_run.py (says what was done).
"""THE CLEAN-UP RECORD (surface_clean.clean -> reading["cleaned"]):

    {"limit_mm", "limit_from": "rule" | "notes", "rung_mm", "closed_edges", "closed_widest_mm", "closed_at_mm": [...],
     "duplicates", "pinholes", "pinhole_widest_mm", "refused": "" | why the next rung was not taken,
     "left_open", "left_widest_mm", "left_at_mm": [...]}

SURF-8, the ladder. The file is first sewn at the clean check's own tolerance (one ten-thousandth of its size), as
before. Free edges of DIFFERENT faces that still lie along each other are a gap. The sewing is then tried again at
each rung of RUNGS times that tolerance, lowest first, never past the limit; after each try every pair of edges it
joined is judged, and the rung is kept only when every join is a gap closed:

- the two edges belong to different faces (one face's own two edges are a slot drawn in it);
- the gap is no wider than GAP_FACE of the narrower of the two faces (a tolerance that reaches across a face would
  sew its two sides together), unless the user stated the tolerance;
- no face is lost and no other free edge disappears.

The first rung that fails is not taken and the climb stops; what it would have closed stays open and is said.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: SURF-8: THE LADDER'S RUNGS, in sew tolerances (one tolerance = cad/validity_brep.SEW_TOLERANCE of the diagonal)
RUNGS = (2.0, 5.0, 10.0, 20.0, 50.0)
#: SURF-8: with no tolerance stated, gaps are closed up to this many sew tolerances - 1/2000 of the part's size,
#: the same reach inside which an edge lying along its neighbour is called an edge that does not meet it (SURF-2's
#: NEAR_SEW): what used to be said as a gap is now closed. The measured numbers behind it are in
#: docs/design/shell/README.md section 12.
GAP_RULE = 5.0
#: SURF-8: a gap is never closed across more than this share of the narrower face beside it (the extraction's own
#: cap for loose faces, cad/midsurface.SEW_CAP)
GAP_FACE = 0.1
#: the ladder is not climbed on a file with more free edges than this (its gaps are said, not closed)
MAX_FREE_EDGES = 20_000
#: points read along a joined edge for its gap
GAP_SAMPLES = 5
#: places kept in the record
MAX_PLACES = 12
#: SURF-8: two faces that leave a joined edge within this angle of each other lie on each other (degrees)
OVERLAP_DEG = 10.0
#: points tried along an edge to find the one nearest a place
INTO_SAMPLES = 32
#: SURF-8: gaps wider than the limit are looked for, and said, up to this many sew tolerances (1/100 of the part's
#: size; at 50 a face pulled 50 tolerances off sat on the edge of the reach and 11 of 74 such files went unsaid)
REPORT_REACH = 100.0
#: SURF-9: a face is another face written twice when its outline, its area and points inside it lie on that face
#: within the sew tolerance
DUP_POINTS = ((0.5, 0.5), (0.3, 0.3), (0.7, 0.3), (0.3, 0.7), (0.7, 0.7))
#: ... and, when those miss the face, the first of a DUP_GRID x DUP_GRID grid that lie inside it
DUP_GRID = 12


def _r(v: float) -> float:
    v = float(v)
    return float(f"{v:.6g}") if math.isfinite(v) else v


# #
# SURF-9: a face written twice
# #

def _inner_points(face) -> list[np.ndarray]:
    """Points of the face's surface that lie inside its outline (a fixed UV grid)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN

    s = BRepAdaptor_Surface(face, True)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    if not all(math.isfinite(x) for x in (u0, u1, v0, v1)):
        return []
    out = []
    # the fixed points first, then a fine grid (a ring's middle is its hole; a strip's lies off a corner)
    grid = [(a / DUP_GRID, b / DUP_GRID) for a in range(1, DUP_GRID) for b in range(1, DUP_GRID)]
    for fu, fv in (*DUP_POINTS, *grid):
        u, v = u0 + fu * (u1 - u0), v0 + fv * (v1 - v0)
        if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-9).State() == TopAbs_IN:
            p = s.Value(u, v)
            out.append(np.array([p.X(), p.Y(), p.Z()]))
            if len(out) >= len(DUP_POINTS):
                break
    return out


def _lies_on(face, other, tol: float) -> bool:
    """Every vertex, every edge's middle and the inner points of `face` lie on `other` within tol."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    pts = _inner_points(face)
    if not pts:
        return False
    m = ms._map(face, TopAbs_EDGE)
    for i in range(1, m.Extent() + 1):
        e = TopoDS.Edge_s(m.FindKey(i))
        try:
            pts += list(sm._edge_points(e, 3))
        except Exception as exc:  # noqa: BLE001 - an edge with no curve does not vote
            logger.debug("surface clean: an edge could not be read (%s)", type(exc).__name__)
    for p in pts:
        d = BRepExtrema_DistShapeShape(ms._vertex(p), other)
        if not d.IsDone() or d.Value() > tol:
            return False
    return True


def without_duplicates(faces: list, tol: float) -> tuple[list[int], list[int]]:
    """SURF-9: (the indices of the faces kept, the indices left out). A face is left out when an EARLIER face of the
    file is the same face: the same area, the same box, and each lying on the other within tol. The first written
    stands. Nothing else is compared: a face lying on another but smaller, larger or shifted is not a duplicate."""
    from meshpipeline.cad import midsurface as ms

    areas = [ms._area(f) for f in faces]
    boxes = [ms._box(f) for f in faces]
    order = sorted(range(len(faces)), key=lambda k: (areas[k], k))
    dropped: set[int] = set()
    for a in range(len(order)):
        i = order[a]
        for b in range(a + 1, len(order)):
            j = order[b]
            # sorted by area: once the areas part, no later face matches
            if areas[j] - areas[i] > 1e-6 * areas[i] + tol * math.sqrt(max(areas[i], 0.0)):
                break
            if i in dropped or j in dropped:
                continue
            if np.abs(boxes[i][0] - boxes[j][0]).max() > 4 * tol or np.abs(boxes[i][1] - boxes[j][1]).max() > 4 * tol:
                continue
            try:
                same = _lies_on(faces[i], faces[j], tol) and _lies_on(faces[j], faces[i], tol)
            except Exception as exc:  # noqa: BLE001 - not judged the same: both are kept
                logger.debug("surface clean: two faces could not be compared (%s)", type(exc).__name__)
                same = False
            if same:
                dropped.add(max(i, j))
    kept = [k for k in range(len(faces)) if k not in dropped]
    return kept, sorted(dropped)


# #
# SURF-8: the ladder
# #

def _owner_map(faces: list):
    """(edge -> faces map of the faces' compound, face index by map index)."""
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    comp = ms._compound(faces)
    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(comp, TopAbs_EDGE, TopAbs_FACE, owners)
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(comp, TopAbs_FACE, fmap)
    place = {fmap.FindIndex(f): k for k, f in enumerate(faces)}
    return comp, owners, fmap, place


def _free(faces: list) -> tuple[list, float]:
    """The free edges of a set of faces (each used by one face once, not degenerate) and their total length."""
    from OCP.BRep import BRep_Tool
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms

    _comp, owners, _fmap, _place = _owner_map(faces)
    out = []
    for i in range(1, owners.Extent() + 1):
        e = TopoDS.Edge_s(owners.FindKey(i))
        if owners.FindFromIndex(i).Extent() == 1 and not BRep_Tool.Degenerated_s(e):
            out.append(e)
    return out, float(sum(ms._length(e) for e in out))


def _apart(a, b) -> float:
    """How far edge a lies from edge b: the farthest of GAP_SAMPLES points along a."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    far = 0.0
    for p in sm._edge_points(a, GAP_SAMPLES):
        d = BRepExtrema_DistShapeShape(ms._vertex(p), b)
        if not d.IsDone():
            return float("inf")
        far = max(far, float(d.Value()))
    return far


def _into(bound, face, at) -> np.ndarray | None:
    """The unit direction from a face's edge INTO the face, read on the edge at the point nearest `at`."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    from meshpipeline.cad import midsurface as ms

    c = BRepAdaptor_Curve(bound)
    t0, t1 = c.FirstParameter(), c.LastParameter()
    best, t_best = float("inf"), 0.5 * (t0 + t1)
    for i in range(1, INTO_SAMPLES):
        t = t0 + (t1 - t0) * i / INTO_SAMPLES
        q = c.Value(t)
        d = (q.X() - at[0]) ** 2 + (q.Y() - at[1]) ** 2 + (q.Z() - at[2]) ** 2
        if d < best:
            best, t_best = d, t
    return ms._into_face(bound, face, t_best)


def _couples(sewn, tol: float) -> tuple[Any, list[dict]]:
    """The sewn faces sewn again at `tol` (nothing kept), and every pair of edges that sewing would join, judged:
    [{"gap", "length", "at", "on": (face, face), "bad": "" | why this join is no gap closed}]. Returns (the sewing,
    the pairs)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    comp, owners, fmap, place = _owner_map(sewn.faces)
    sewing = BRepBuilderAPI_Sewing(float(tol), True, True, True, False)
    sewing.Add(comp)
    sewing.Perform()
    out: list[dict] = []
    for i in range(1, int(sewing.NbContigousEdges()) + 1):
        secs = [TopoDS.Edge_s(s) for s in sewing.ContigousEdgeCouple(i)]
        row: dict = {"gap": 0.0, "length": float(sum(ms._length(s) for s in secs)), "at": None, "on": (), "bad": ""}
        out.append(row)
        if len(secs) != 2:
            row["bad"] = "more than two edges would be joined along one line"
            continue
        on: list[int] = []
        bounds = []
        for s in secs:
            bound = TopoDS.Edge_s(sewing.SectionToBoundary(s)) if sewing.IsSectionBound(s) else s
            if not owners.Contains(bound) or owners.FindFromKey(bound).Extent() != 1:
                row["bad"] = "a joined edge could not be traced to one face"
                break
            on.append(place[fmap.FindIndex(owners.FindFromKey(bound).First())])
            bounds.append(bound)
        if row["bad"]:
            continue
        row["on"] = (on[0], on[1])
        mid = sm._edge_points(secs[0], 3)[1]
        row["at"] = [float(c) for c in mid]
        row["gap"] = float(min(_apart(secs[0], secs[1]), _apart(secs[1], secs[0])))
        if on[0] == on[1]:
            row["bad"] = "two edges of one face would be joined (a slot or slit drawn in that face)"
            continue
        if not math.isfinite(row["gap"]) or row["gap"] > 1.5 * tol + 1e-12:
            row["bad"] = "edges farther apart than the tolerance would be joined"
            continue
        # TWO FACES LYING ON EACH OTHER (a face written twice a little off, a patch laid over the sheet) leave the
        # joined edge to the SAME side: sewn along their rims they would make a closed pocket, not a sheet
        try:
            da = _into(bounds[0], sewn.faces[on[0]], mid)
            db = _into(bounds[1], sewn.faces[on[1]], mid)
        except Exception as exc:  # noqa: BLE001 - not read: the join is judged by its gap alone
            logger.debug("surface clean: the faces beside a gap could not be read (%s)", type(exc).__name__)
            da = db = None
        if da is not None and db is not None and float(np.dot(da, db)) > math.cos(math.radians(OVERLAP_DEG)):
            row["bad"] = "two faces lying on each other would be joined along their rims"
    return sewing, out


def try_rung(sewn, tol: float, *, face_cap: bool = True) -> tuple[Any, dict]:
    """SURF-8: ONE RUNG. The sewn faces sewn again at `tol`, and every pair of edges that sewing joined judged.
    Returns (the new Sewn, the rung's record) - the Sewn is None when the rung is refused (record["refused"] says
    why) or joined nothing (record["joined"] == 0).
    record: {"tol", "joined", "widest", "places": [(gap, middle)], "refused"} in the shape's own units."""
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    rec: dict[str, Any] = {"tol": float(tol), "joined": 0, "widest": 0.0, "places": [], "refused": ""}
    _free_before, length_before = _free(sewn.faces)
    sewing, pairs = _couples(sewn, tol)
    n = len(pairs)
    if n == 0:
        return None, rec
    widths: dict[int, float] = {}

    def width(k: int) -> float:
        if k not in widths:
            f = sewn.faces[k]
            per = ms._length(f)
            widths[k] = 2.0 * ms._area(f) / per if per > 0 else 0.0
        return widths[k]

    for row in pairs:
        if row["bad"]:
            rec["refused"] = row["bad"]
            return None, rec
        if face_cap and row["gap"] > GAP_FACE * min(width(row["on"][0]), width(row["on"][1])):
            rec["refused"] = "a gap is wide against the narrow face beside it"
            return None, rec
        rec["places"].append((row["gap"], row["at"]))
    merged = float(sum(row["length"] for row in pairs))
    result = sewing.SewedShape()
    if result is None or result.IsNull() or int(sewing.NbDeletedFaces()) > 0:
        rec["refused"] = "a face would be lost"
        return None, rec
    index = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(result, TopAbs_FACE, index)
    faces = []
    seen: set[int] = set()
    for f in sewn.faces:
        g = sewing.ModifiedSubShape(f)
        k = index.FindIndex(g) if g is not None and not g.IsNull() else 0
        if k <= 0 or k in seen:
            rec["refused"] = "a face would be lost"
            return None, rec
        seen.add(k)
        faces.append(TopoDS.Face_s(index.FindKey(k)))
    area_before = sum(ms._area(f) for f in sewn.faces)
    area_after = sum(ms._area(f) for f in faces)
    if abs(area_after - area_before) > 1e-6 * area_before + tol * 0.5 * merged:
        rec["refused"] = "the surface's area would change"
        return None, rec
    _free_after, length_after = _free(faces)
    # every free edge that is gone was one of a joined pair: nothing else may disappear (a short edge swallowed, a
    # small opening pinched shut)
    slack = 1e-9 * length_before + 4.0 * tol * (n + 1)
    if length_before - merged - length_after > slack:
        rec["refused"] = "a free edge that is no gap would disappear"
        return None, rec
    rec.update(joined=n, widest=max(g for g, _p in rec["places"]))
    rec["places"].sort(key=lambda x: (-x[0], x[1]))
    return sm.Sewn(faces, list(sewn.names), sm._sheets(faces), sewn.tol, sewn.diag), rec


def wider(sewn, reach: float) -> dict:
    """SURF-8: THE GAPS A WIDER TOLERANCE WOULD CLOSE, found and not closed: one trial sewing at `reach`, and the
    pairs it would join that are gaps by the same judgement (different faces, not lying on each other).
    {"count", "widest", "places"} in the shape's own units - what is said to the user as left open."""
    out: dict[str, Any] = {"count": 0, "widest": 0.0, "places": []}
    if not _free(sewn.faces)[0] or len(_free(sewn.faces)[0]) > MAX_FREE_EDGES:
        return out
    try:
        _sewing, pairs = _couples(sewn, reach)
    except Exception as exc:  # noqa: BLE001 - nothing more is said
        logger.info("surface clean: the wider gaps were not looked for (%s)", type(exc).__name__)
        return out
    good = sorted(((r["gap"], r["at"]) for r in pairs if not r["bad"] and r["at"] is not None),
                  key=lambda x: (-x[0], x[1]))
    if good:
        out.update(count=len(good), widest=float(good[0][0]), places=good)
        return out
    # the sewing pairs nothing it may join (a pair it would refuse hides the rest): free edges lying along another
    # face within the reach are found one by one, as SURF-2 finds them within its own
    try:
        from meshpipeline.cad import surface_model as sm

        sheets = sm.measure(sewn, mm_per_unit=1.0, reach=reach)
    except Exception as exc:  # noqa: BLE001 - nothing more is said
        logger.info("surface clean: the wider gaps were not measured (%s)", type(exc).__name__)
        return out
    n = sum(int(s["unmet_edges"]) for s in sheets)
    if n:
        places = [(0.0, list(p)) for s in sheets for p in s["unmet_at_mm"]]
        out.update(count=n, widest=max(float(s.get("unmet_widest_mm") or 0.0) for s in sheets), places=places)
    return out


def ladder(sewn, limit: float, *, stated: bool = False) -> tuple[Any, dict]:
    """SURF-8: climb the ladder from the sewn faces: each rung of RUNGS (times the sew tolerance) up to `limit` (the
    shape's own units), and the limit itself as the last rung. A rung that joins nothing is passed over; one that
    is refused stops the climb. Returns (the Sewn to use, {"rung", "joined", "widest", "places", "refused"})."""
    out: dict[str, Any] = {"rung": 0.0, "joined": 0, "widest": 0.0, "places": [], "refused": ""}
    free, _length = _free(sewn.faces)
    if not free or limit <= sewn.tol:
        return sewn, out
    if len(free) > MAX_FREE_EDGES:
        out["refused"] = "the file has too many open edges to stitch by rule"
        return sewn, out
    rungs = sorted({r * sewn.tol for r in RUNGS if r * sewn.tol < limit} | {float(limit)})
    here = sewn
    for tol in rungs:
        try:
            got, rec = try_rung(here, tol, face_cap=not stated)
        except Exception as exc:  # noqa: BLE001 - the rung is not taken; what is open is said
            logger.info("surface clean: a sewing rung failed (%s)", type(exc).__name__)
            out["refused"] = "the sewing failed at the next tolerance"
            break
        if rec["refused"]:
            out["refused"] = rec["refused"]
            break
        if got is None:
            continue
        here = got
        out["rung"] = float(tol)
        out["joined"] += int(rec["joined"])
        out["widest"] = max(out["widest"], float(rec["widest"]))
        out["places"] += rec["places"]
        if not _free(here.faces)[0]:
            break
    out["places"].sort(key=lambda x: (-x[0], x[1]))
    return here, out


# #
# SURF-10: pinholes
# #

def _across(wire) -> float:
    """How big a loop is across: the farthest two of the points read along its edges."""
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    em = ms._map(wire, TopAbs_EDGE)
    pts = []
    for j in range(1, em.Extent() + 1):
        try:
            pts.append(sm._edge_points(TopoDS.Edge_s(em.FindKey(j)), 9))
        except Exception:  # noqa: BLE001, S112 - an edge with no curve adds no point
            continue
    if not pts:
        return float("inf")
    p = np.vstack(pts)
    d = p[:, None, :] - p[None, :, :]
    return float(np.sqrt((d * d).sum(axis=2)).max())


def fill_pinholes(sewn, limit: float) -> tuple[Any, list[float]]:
    """SURF-10: every INNER loop of a face that is no bigger across than `limit` is taken off its face: the hole is
    filled with the face's own surface, which was always there under it. Only a loop whose every edge is free (the
    hole is open, nothing is attached to its rim). Returns (the Sewn, the sizes of the holes filled)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FORWARD, TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm

    _comp, owners, _fmap, _place = _owner_map(sewn.faces)
    faces = list(sewn.faces)
    filled: list[float] = []
    for k, face in enumerate(sewn.faces):
        f = TopoDS.Face_s(face.Oriented(TopAbs_FORWARD))      # the wires as the face stores them
        wires = ms._map(f, TopAbs_WIRE)
        if wires.Extent() < 2:
            continue
        outer = BRepTools.OuterWire_s(f)
        drop = []
        for i in range(1, wires.Extent() + 1):
            w = wires.FindKey(i)
            if w.IsSame(outer):
                continue
            lo, hi = ms._box(w)
            if float(np.max(hi - lo)) > limit:           # the box first: most loops are far bigger
                continue
            across = _across(w)
            if across > limit:
                continue
            em = ms._map(w, TopAbs_EDGE)
            if any(not owners.Contains(em.FindKey(j)) or owners.FindFromKey(em.FindKey(j)).Extent() != 1
                   for j in range(1, em.Extent() + 1)):
                continue
            drop.append((w, across))
        if not drop:
            continue
        try:
            # the face rebuilt on its own surface with every wire but the pinholes'
            empty = TopoDS.Face_s(f.EmptyCopied())
            b = BRep_Builder()
            gone = [w for w, _a in drop]
            for i in range(1, wires.Extent() + 1):
                w = wires.FindKey(i)
                if any(w.IsSame(g) for g in gone):
                    continue
                b.Add(empty, w)
            grown = ms._area(empty) - ms._area(f)
            if grown < 0 or grown > sum(a * a for _w, a in drop) + 1e-9 * ms._area(f):
                continue
            faces[k] = TopoDS.Face_s(empty.Oriented(face.Orientation()))
            filled += [a for _w, a in drop]
        except Exception as exc:  # noqa: BLE001 - the face as drawn
            logger.debug("surface clean: a pinhole was not filled (%s)", type(exc).__name__)
    if not filled:
        return sewn, []
    return sm.Sewn(faces, list(sewn.names), sm._sheets(faces), sewn.tol, sewn.diag), sorted(filled)


def clean(sewn, *, mm_per_unit: float, limit_mm: float | None = None,
          holes_mm: float | None = None) -> tuple[Any, dict]:
    """SURF-8 and SURF-10 on a surface part's sewn faces: (the Sewn to mesh, the clean-up record in mm). `limit_mm`:
    the stitch tolerance the user's notes state (None: the rule's, GAP_RULE sew tolerances). `holes_mm`: the size
    under which the user asked for holes to be filled (None: only pinholes no bigger than the gaps closed)."""
    mm = float(mm_per_unit)
    stated = limit_mm is not None and limit_mm > 0
    limit = float(limit_mm) / mm if limit_mm is not None and stated else GAP_RULE * sewn.tol
    out, rec = ladder(sewn, limit, stated=stated)
    hole_limit = float(holes_mm) / mm if holes_mm else limit
    out, holes = fill_pinholes(out, hole_limit)
    more = wider(out, max(REPORT_REACH * sewn.tol, 2.0 * limit))
    cleaned = {"limit_mm": _r(limit * mm), "limit_from": "notes" if stated else "rule",
               "rung_mm": _r(rec["rung"] * mm), "closed_edges": int(rec["joined"]),
               "closed_widest_mm": _r(rec["widest"] * mm),
               "closed_at_mm": [[_r(c * mm) for c in p] for _g, p in rec["places"][:MAX_PLACES]],
               "pinholes": len(holes), "pinhole_widest_mm": _r(max(holes) * mm) if holes else 0.0,
               "holes_limit_mm": _r(hole_limit * mm), "holes_from": "notes" if holes_mm else "rule",
               "refused": rec["refused"],
               "wider_gaps": int(more["count"]), "wider_widest_mm": _r(more["widest"] * mm),
               "wider_at_mm": [[_r(c * mm) for c in p] for _g, p in more["places"][:MAX_PLACES]]}
    return out, cleaned


__all__ = ["GAP_FACE", "GAP_RULE", "REPORT_REACH", "RUNGS", "clean", "fill_pinholes", "ladder", "try_rung", "wider",
           "without_duplicates"]
