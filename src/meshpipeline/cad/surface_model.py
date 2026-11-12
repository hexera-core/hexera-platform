# Responsibility: Read a CAD file that is a SURFACE, not a solid (SURF-1..3): an open shell or sheet body - a stamped
# part exported as its mid-surface or one skin. Tell it from a solid and from a closed skin with a small gap, sew its
# faces within the clean check's own tight tolerance with every free edge left free, find the edges that do not meet,
# and write the SAME record and surface the mid-surface extractor writes (cad/midsurface: shell.json +
# midsurface.brep), so the shell mesher, the solver files and the delivery are the ones every shell mesh uses. The
# surface IS the mid-surface: nothing is offset, extended or healed.
# Boundaries: geometry only, deterministic, no model call, no persistence beyond the two files it is asked to write.
# It meshes nothing and never changes the CAD beyond the sewing. A file with a solid, or whose faces close, is not
# its business (kind "solid" / "closed": engines/fea_driver.solids_from_shells, MESH-1). The CAD repair stage
# (cad/repair) is never called and never edited here.
# Collaborates with: cad/validity_brep.py (the sew tolerance and the sewing), cad/midsurface.py (the record's shape,
# the face helpers), engines/surface_run.py (runs it), engines/gmsh/shell_adapter.py (reads what it writes).
"""THE SURFACE READING (surface_model.read):

    {"kind": "surface" | "gap" | "closed" | "solid" | "empty", "why", "faces", "sew_tolerance_mm", "diag_mm",
     "open_share", "said_shell",
     "sheets": [{"part": "Part_1", "faces": ["Face_1", ...], "area_mm2", "size_mm", "free_edges", "free_length_mm",
                 "loops": [{"length_mm", "span_mm2", "edges", "at_mm", "seam"}], "open_share",
                 "unmet_edges", "unmet_at_mm": [...], "closed": bool}]}

SURF-1, the rule. A file is a surface part when it holds NO solid, its faces do not close into one after the clean
check's own sewing, and it is not a closed skin with a small gap. The measure is the file's OPEN SHARE: the area its
free boundary loops span (each loop's vector area, |1/2 sum r x dr| - exact for a flat loop, never more than any
surface that closes it) over its surface area. A flat sheet spans its whole area; a pressed panel, a bracket, a
channel, a cup, a tray, an open tube span a share of theirs; a small missing face spans a sliver. A loop whose edges
lie along a neighbouring face (a seam the sewing did not close, SURF-2) or that spans next to nothing for its length
(a slit) spans nothing: it is a crack, not an opening.

A file whose openings span at most GAP_SHARE of its surface is a closed skin with a gap - CAD repair's business, as
before. Anything else is a surface part. The user's own words (shell, mid-surface, a thickness) settle a file under
the share as a surface too: by shape alone a tube 50 diameters long cannot be told from a rod with its ends missing.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: SURF-1: a file whose openings span at most this share of its surface is a closed skin with a gap, not a surface
#: part. Measured on 86 public closed solids (docs/design/shell/README.md section 11): a crack reads 0; one face
#: left out reads under it for six in ten faces; the least open surface parts drawn for the lab (an open tube 40
#: diameters long, a tank skin with a nozzle hole) read 1.2% and 1.3%.
GAP_SHARE = 0.01
#: a loop spanning under this share of its own length squared over 4 pi (a circle spans 1) is a slit: no opening
SLIT_SHARE = 1e-4
#: SURF-2: a free edge whose every sample point lies within NEAR_SEW sew tolerances of another face is an edge
#: that does not meet its neighbour (a seam wider than the sew tolerance, a rib that stops short): counted and
#: placed, never closed.
NEAR_SEW = 5.0
#: SURF-2: the fuse that joins a rib to the face its foot lands inside is tried on at most this many faces, and kept
#: only when the faces' area is the same within this share
IMPRINT_MAX_FACES = 3000
IMPRINT_AREA = 1e-6
#: points read along an edge: for its loop's span, and (the inner ones) for the edges that do not meet
EDGE_POINTS = 16
NEAR_SAMPLES = (0.25, 0.5, 0.75)
#: places kept per sheet
MAX_PLACES = 12
#: SURF-13: the open places named for the mesh to close (each with its faces)
MAX_NAMED = 200


def _r(v: float) -> float:
    v = float(v)
    return float(f"{v:.6g}") if math.isfinite(v) else v


def _edge_points(edge, n: int = EDGE_POINTS) -> np.ndarray:
    """n points along an edge, in the edge's own direction (first vertex to last)."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_REVERSED

    a = BRepAdaptor_Curve(edge)
    t0, t1 = a.FirstParameter(), a.LastParameter()
    pts = []
    for i in range(n):
        p = a.Value(t0 + (t1 - t0) * i / (n - 1))
        pts.append((p.X(), p.Y(), p.Z()))
    out = np.asarray(pts, dtype=float)
    return out[::-1] if edge.Orientation() == TopAbs_REVERSED else out


def _loops(free: list, vmap) -> list[dict]:
    """The free edges chained into loops through the vertices they share: [{"edges": [edge...], "points": (n, 3),
    "index": [each edge's place in `free`]}].
    A chain that does not come back to its start (a pinched or broken boundary) is still returned, as walked."""
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS_Vertex

    ends: list[tuple[int, int]] = []
    for e in free:
        a, b = TopoDS_Vertex(), TopoDS_Vertex()
        TopExp.Vertices_s(e, a, b, True)
        ends.append((vmap.FindIndex(a), vmap.FindIndex(b)))
    at: dict[int, list[int]] = {}
    for k, (a, b) in enumerate(ends):
        at.setdefault(a, []).append(k)
        if b != a:
            at.setdefault(b, []).append(k)
    used = [False] * len(free)
    out = []
    for start in range(len(free)):
        if used[start]:
            continue
        used[start] = True
        chain, pts, idx = [free[start]], [_edge_points(free[start])], [start]
        first, here = ends[start]
        while here != first:
            nxt = next((k for k in at.get(here, []) if not used[k]), None)
            if nxt is None:
                break
            used[nxt] = True
            p = _edge_points(free[nxt])
            a, b = ends[nxt]
            if a != here:
                p, a, b = p[::-1], b, a
            chain.append(free[nxt])
            idx.append(nxt)
            pts.append(p)
            here = b
        out.append({"edges": chain, "points": np.vstack(pts), "index": idx})
    return out


def _span(points: np.ndarray) -> np.ndarray:
    """The vector area a closed loop of points spans (1/2 sum r_i x r_i+1 about its middle): its size is the area
    spanned."""
    p = points - points.mean(axis=0)
    return 0.5 * np.cross(p, np.roll(p, -1, axis=0)).sum(axis=0)


def _components(n: int, links: list[tuple[int, int]]) -> list[list[int]]:
    parent = list(range(n))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in links:
        ra, rb = root(a), root(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(root(i), []).append(i)
    return [groups[k] for k in sorted(groups)]


class Sewn:
    """The file's faces after the sewing: `faces` (the sewn face of each face of the file that survived, in the
    file's own order), `names` (Face_k, k the face's number in the file), `sheets` (lists of indices into
    `faces`: the faces joined through shared edges), and the tolerance used (the shape's own units)."""

    def __init__(self, faces: list, names: list[str], sheets: list[list[int]], tol: float, diag: float) -> None:
        self.faces, self.names, self.sheets, self.tol, self.diag = faces, names, sheets, tol, diag


def sew(shape, *, junctions: bool = False, leave_out: frozenset[int] = frozenset()) -> Sewn | None:
    """SURF-2: the file's faces sewn as the clean check sews them (cad/validity_brep: BRepBuilderAPI_Sewing at
    SEW_TOLERANCE of the diagonal - edges that meet within it become one edge, every other edge stays free) and
    grouped into sheets. With `junctions`, an edge three or more faces meet along (a rib on a plate, a T) is sewn
    into one edge too - the same tolerance, the sewing's own non-manifold mode. `leave_out`: the faces (their
    place in the file, from 0) that are another face written twice (SURF-9); the others keep their own numbers.
    None when the file holds no face with a size."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad.validity_brep import SEW_TOLERANCE, _bbox

    before = ms._faces(shape)
    lo, hi = _bbox(shape)
    if not before or lo is None:
        return None
    diag = float(np.linalg.norm(hi - lo))
    if not math.isfinite(diag) or diag <= 0:
        return None
    tol = SEW_TOLERANCE * diag
    sewing = BRepBuilderAPI_Sewing(tol, True, True, True, bool(junctions))
    sewing.Add(ms._compound([f for k, f in enumerate(before) if k not in leave_out]) if leave_out else shape)
    sewing.Perform()
    sewn = sewing.SewedShape()
    if sewn is None or sewn.IsNull():
        return None
    index = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(sewn, TopAbs_FACE, index)
    faces, names = [], []
    seen: set[int] = set()
    for k, f in enumerate(before):
        if k in leave_out:
            continue
        g = sewing.ModifiedSubShape(f)
        i = index.FindIndex(g) if g is not None and not g.IsNull() else 0
        if i <= 0 or i in seen:
            continue                              # a face the sewing dropped (degenerate), or met twice
        seen.add(i)
        faces.append(TopoDS.Face_s(index.FindKey(i)))
        names.append(f"Face_{k + 1}")
    if not faces:
        return None
    return Sewn(faces, names, _sheets(faces), tol, diag)


def _sheets(faces: list) -> list[list[int]]:
    """The faces grouped into sheets: those joined through the edges they share (indices into `faces`)."""
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
    links = []
    for i in range(1, owners.Extent() + 1):
        on = sorted({place[fmap.FindIndex(f)] for f in owners.FindFromIndex(i)})
        links += [(on[0], o) for o in on[1:]]
    return _components(len(faces), links)


def imprint(sewn: Sewn) -> Sewn | None:
    """SURF-2: a rib's foot that lands INSIDE a face has no edge there to sew to. The sewn faces are fused
    (OpenCASCADE's general fuse, as the mid-surface extractor joins its mid-faces, SHELL-4) with the sew tolerance
    as its fuzzy value: a face another face's edge lies on is split or scored along it, so the two share that edge.
    No face is moved, added or dropped: each piece keeps the CAD face it came from (its name), and the area is the
    same. None when the fuse fails or loses a face - the caller keeps the sewn faces and says the edges do not meet."""
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    if len(sewn.faces) < 2 or len(sewn.faces) > IMPRINT_MAX_FACES:
        return None
    try:
        bop = BOPAlgo_Builder()
        for f in sewn.faces:
            bop.AddArgument(f)
        bop.SetRunParallel(False)
        bop.SetNonDestructive(True)
        bop.SetFuzzyValue(float(sewn.tol))
        bop.Perform()
        if bop.HasErrors():
            return None
        index = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(bop.Shape(), TopAbs_FACE, index)
        faces, names = [], []
        seen: set[int] = set()
        for f, name in zip(sewn.faces, sewn.names, strict=True):
            images = [TopoDS.Face_s(x) for x in bop.Modified(f)] or ([] if bop.IsDeleted(f) else [f])
            kept = 0
            for g in images:
                i = index.FindIndex(g)
                if i > 0 and i not in seen:
                    seen.add(i)
                    faces.append(TopoDS.Face_s(index.FindKey(i)))
                    names.append(name)
                    kept += 1
            if not kept:
                return None
        before, after = sum(ms._area(f) for f in sewn.faces), sum(ms._area(f) for f in faces)
        if abs(after - before) > IMPRINT_AREA * before:
            return None
        return Sewn(faces, names, _sheets(faces), sewn.tol, sewn.diag)
    except Exception as exc:  # noqa: BLE001 - not joined: the edges are said as not meeting
        logger.info("surface: the faces could not be fused at their junctions (%s)", type(exc).__name__)
        return None


def _unmet(free: list, own: list[int], faces: list, boxes: list, reach: float,
           far: list[float] | None = None) -> list[np.ndarray | None]:
    """SURF-2: for each free edge, its middle when it lies along another face (every sample point within `reach` of
    a face that is not the edge's own: a seam the sewing did not close, a rib that does not reach its plate), else
    None. `far` (when given) is filled with how far each such edge lies from that face (the farthest sample)."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    from meshpipeline.cad import midsurface as ms

    out: list[np.ndarray | None] = []
    for e, mine in zip(free, own, strict=True):
        pts = _edge_points(e)
        probe = [pts[int(round(s * (len(pts) - 1)))] for s in NEAR_SAMPLES]
        found = None
        for k, f in enumerate(faces):
            if k == mine:
                continue
            lo, hi = boxes[k]
            if any(np.any(p < lo - reach) or np.any(p > hi + reach) for p in probe):
                continue
            near, widest = True, 0.0
            for p in probe:
                d = BRepExtrema_DistShapeShape(ms._vertex(p), f)
                if not d.IsDone() or d.Value() > reach:
                    near = False
                    break
                widest = max(widest, float(d.Value()))
            if near:
                found = probe[1]
                if far is not None:
                    far.append(widest)
                break
        out.append(found)
    return out


def measure(sewn: Sewn, *, mm_per_unit: float, reach: float | None = None) -> list[dict]:
    """Each sheet of the sewn file, measured (SURF-1, SURF-2): its area and size, its free edges chained into loops
    with the area each spans, its open share, and the edges that do not meet their neighbour. `reach` (the shape's
    own units): how near a neighbouring face an edge must lie to count as not meeting it - NEAR_SEW sew tolerances
    unless a wider stitch tolerance was stated (SURF-8)."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    mm = float(mm_per_unit)
    comp = ms._compound(sewn.faces)
    vmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(comp, TopAbs_VERTEX, vmap)
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(comp, TopAbs_FACE, fmap)
    place = {fmap.FindIndex(f): k for k, f in enumerate(sewn.faces)}
    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(comp, TopAbs_EDGE, TopAbs_FACE, owners)
    free_of: dict[int, list] = {}
    own_of: dict[int, list[int]] = {}
    shared: dict[int, int] = {}
    junction: dict[int, int] = {}
    edges_in: dict[int, int] = {}
    sheet_of = {k: s for s, ks in enumerate(sewn.sheets) for k in ks}
    for i in range(1, owners.Extent() + 1):
        e = TopoDS.Edge_s(owners.FindKey(i))
        if BRep_Tool.Degenerated_s(e):
            continue
        on = sorted({place[fmap.FindIndex(f)] for f in owners.FindFromIndex(i)})
        uses = owners.FindFromIndex(i).Extent()
        s = sheet_of[on[0]]
        edges_in[s] = edges_in.get(s, 0) + 1
        if uses == 1:
            free_of.setdefault(s, []).append(e)
            own_of.setdefault(s, []).append(on[0])
        elif uses == 2:
            shared[s] = shared.get(s, 0) + 1          # (a closed face's own seam counts here: it is not free)
        else:
            junction[s] = junction.get(s, 0) + 1
    boxes = [ms._box(f) for f in sewn.faces]
    reach = max(NEAR_SEW * sewn.tol, float(reach or 0.0))
    areas = [sum(ms._area(sewn.faces[k]) for k in ks) for ks in sewn.sheets]
    out = []
    for s, ks in enumerate(sewn.sheets):
        area = areas[s]
        free = free_of.get(s, [])
        far: list[float] = []
        unmet = _unmet(free, own_of.get(s, []), sewn.faces, boxes, reach, far) if free else []
        rows: list[dict] = []
        spanned = 0.0
        for lp in _loops(free, vmap):
            pts = lp["points"]
            size = float(np.linalg.norm(_span(pts)))
            length = sum(ms._length(e) for e in lp["edges"])
            # a loop most of whose edges lie along a neighbouring face is a seam that did not sew, and one that
            # spans next to nothing for its length is a slit: neither is an opening
            seam = (sum(1 for i in lp["index"] if unmet[i] is not None) * 2 >= len(lp["index"])
                    or size <= SLIT_SHARE * length * length / (4.0 * math.pi))
            span_mm2 = 0.0 if seam else _r(size * mm * mm)
            spanned += span_mm2
            rows.append({"length_mm": _r(length * mm), "span_mm2": span_mm2, "edges": len(lp["edges"]),
                         "at_mm": [_r(c * mm) for c in pts.mean(axis=0)], "seam": bool(seam)})
        rows.sort(key=lambda r: (-float(r["length_mm"]), list(r["at_mm"])))
        area_mm2 = area * mm * mm
        places = [p for p in unmet if p is not None]
        out.append({"part": f"Part_{s + 1}", "faces": list(dict.fromkeys(sewn.names[k] for k in ks)),
                    "area_mm2": _r(area_mm2),
                    "size_mm": _r(math.sqrt(area_mm2)), "edges": edges_in.get(s, 0), "shared_edges": shared.get(s, 0),
                    "junction_edges": junction.get(s, 0), "free_edges": len(free),
                    "free_length_mm": _r(sum(ms._length(e) for e in free) * mm),
                    "loops": rows[:MAX_PLACES], "loop_count": len(rows),
                    "open_share": _r(spanned / area_mm2) if area_mm2 > 0 else 0.0,
                    "unmet_edges": len(places), "unmet_at_mm": [[_r(c * mm) for c in p] for p in places[:MAX_PLACES]],
                    "unmet_widest_mm": _r(max(far) * mm) if far else 0.0,
                    "closed": not free})
    return out


#: SURF-8: AN OPEN PLACE, one definition whatever tolerance was asked: a run of free edges (joined end to end) each of
#: which lies along a face that is not its own within OPEN_REACH sew tolerances (1/100 of the part's size). Every
#: other free edge is the sheet's own rim. Measured the same way after any clean-up, so a stated tolerance and the
#: rule's can be compared, and the user and the record see the same count.
OPEN_REACH = 100.0


#: SURF-8: an edge runs ALONGSIDE a face when it is parallel to it (its direction within ALONG_DEG of the face's
#: surface where it is nearest) and does not merely leave a corner it shares with that face (distances growing from
#: the shared corner, as two rim edges meeting at a bend or running on from one another do)
ALONG_DEG = 15.0
FROM_CORNER = 2.0
#: ... but an edge whose distance grows under this much per unit of its length (about 6 degrees) is a crack, not a rim
WEDGE_SLOPE = 0.1
#: ... and a face bending by up to this much across a gap is the same sheet continued (the edge runs alongside it)
SAME_SHEET_DEG = 45.0


def _same_sheet(sas, srf, q, n) -> bool:
    """SURF-8: whether the edge's own face at probe q faces within SAME_SHEET_DEG of the neighbour's normal n - the
    same sheet continued across the gap, bending on the way (a bead's flank, a stamped curve): the edge lies in its own
    face, so it runs alongside a neighbour that only bends away from that face. (11 Nov: a 1 mm gap across a curved
    stamped panel's slanted cut was not counted - the neighbour's surface 1 mm across a bead turned 20 degrees.)"""
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.gp import gp_Pnt

    if sas is None:
        return False
    uv = sas.ValueOfUV(gp_Pnt(*map(float, q)), 1e-7)
    pr = BRepLProp_SLProps(srf, uv.X(), uv.Y(), 1, 1e-9)
    if not pr.IsNormalDefined():
        return False
    m = pr.Normal()
    return abs(m.X() * n.X() + m.Y() * n.Y() + m.Z() * n.Z()) >= np.cos(np.radians(SAME_SHEET_DEG))


def _alongside(edge, probe: list, dists: list, feet: list, face, tol: float, own=None, faces_at: list | None = None) -> bool:
    """SURF-8: whether a free edge lying within reach of `face` (its probe points at `dists` from it, nearest at
    `feet`) runs alongside it - a gap, an overlap, an edge stopping short - rather than meeting it at a corner.
    `own`: the edge's own face. `faces_at`: the face nearest each probe when they are not one face (an edge stopping
    short of a sheet drawn in several patches): each probe is judged against its own."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopAbs import TopAbs_REVERSED, TopAbs_VERTEX
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms

    # 1. parallel: the edge's direction lies in the face's tangent plane where it is nearest
    c = BRepAdaptor_Curve(edge)
    t0, t1 = c.FirstParameter(), c.LastParameter()
    per = list(faces_at) if faces_at else [face] * len(probe)
    sas_of = {id(f): (ShapeAnalysis_Surface(BRep_Tool.Surface_s(f)), BRepAdaptor_Surface(f, False)) for f in per}
    # the probes are taken in the edge's own direction (_edge_points): a reversed edge's run from its curve's end
    rev = edge.Orientation() == TopAbs_REVERSED
    own_sas = ShapeAnalysis_Surface(BRep_Tool.Surface_s(own)) if own is not None else None
    own_srf = BRepAdaptor_Surface(own, False) if own is not None else None
    for j, (q, foot) in enumerate(zip(probe, feet, strict=True)):
        t = (t1 - (t1 - t0) * NEAR_SAMPLES[j]) if rev else (t0 + (t1 - t0) * NEAR_SAMPLES[j])
        p, v = gp_Pnt(), gp_Vec()
        c.D1(t, p, v)
        if v.Magnitude() < 1e-14:
            continue
        sas, srf = sas_of[id(per[j])]
        uv = sas.ValueOfUV(gp_Pnt(*map(float, foot)), 1e-7)
        pr = BRepLProp_SLProps(srf, uv.X(), uv.Y(), 1, 1e-9)
        if not pr.IsNormalDefined():
            continue
        n = pr.Normal()
        sin = abs(v.X() * n.X() + v.Y() * n.Y() + v.Z() * n.Z()) / v.Magnitude()
        if sin > np.sin(np.radians(ALONG_DEG)) and not _same_sheet(own_sas, own_srf, q, n):
            return False
    # 2. not leaving a shared corner: the edge shares a vertex with the face and the distance grows from it
    near_ends = []
    em = ms._map(edge, TopAbs_VERTEX)
    fpts = []
    for f in {id(f): f for f in per}.values():
        fv = ms._map(f, TopAbs_VERTEX)
        for k in range(1, fv.Extent() + 1):
            w = BRep_Tool.Pnt_s(TopoDS.Vertex_s(fv.FindKey(k)))
            fpts.append(np.array([w.X(), w.Y(), w.Z()]))
    for k in range(1, em.Extent() + 1):
        q = BRep_Tool.Pnt_s(TopoDS.Vertex_s(em.FindKey(k)))
        qa = np.array([q.X(), q.Y(), q.Z()])
        if any(float(np.linalg.norm(qa - w)) <= 2.0 * tol for w in fpts):
            near_ends.append(qa)
    if near_ends and len(dists) == len(probe) and min(dists) > 0:
        lo, hi = min(dists), max(dists)
        # ... unless it opens from that corner as a crack does, at a slant of a few degrees: two edges meant to be one,
        # joined at one end (a face slid along its neighbour, partly stitched) - a sheet's own rim leaves its
        # neighbour's corner at the rim's own angle, its distance growing about as fast as the edge runs (11 Nov: two
        # matrix files left a crack 0.03 mm wide along 6 mm open and unsaid)
        slope = (hi - lo) / max(0.5 * ms._length(edge), 1e-300)
        if hi >= FROM_CORNER * lo and slope > WEDGE_SLOPE:
            return False
    return True


def _along_several(e, mine: int, probe: list, faces: list, boxes: list, reach: float,
                   tol: float) -> tuple[float, int] | None:
    """SURF-8: a free edge lying along SEVERAL faces, none of which holds all its probes within `reach`: (how far it
    lies, the face nearest its middle) when every probe lies within reach of some other face and runs alongside it,
    and the probes are not all nearest one face (that face was judged on its own); else None."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad import midsurface as ms

    def corners(shape) -> list[np.ndarray]:
        vm = ms._map(shape, TopAbs_VERTEX)
        return [np.array([(p := BRep_Tool.Pnt_s(TopoDS.Vertex_s(vm.FindKey(i)))).X(), p.Y(), p.Z()])
                for i in range(1, vm.Extent() + 1)]

    ends = corners(e)
    meets: dict[int, bool] = {}
    dists, feet, at = [], [], []
    for q in probe:
        best: tuple[float, int, np.ndarray] | None = None
        for k, f in enumerate(faces):
            if k == mine:
                continue
            lo, hi = boxes[k]
            if np.any(q < lo - reach) or np.any(q > hi + reach):
                continue
            # a face the edge meets at a corner is its own sheet running on (a rim's next patch), never what it
            # stops short of (12 Nov: a panel's outline along 6 mm bead strips read as a 3.7 mm gap)
            if k not in meets:
                meets[k] = any(float(np.linalg.norm(c - w)) <= 2.0 * tol for c in ends for w in corners(f))
            if meets[k]:
                continue
            d = BRepExtrema_DistShapeShape(ms._vertex(q), f)
            if not d.IsDone() or not d.NbSolution():
                continue
            dq = float(d.Value())
            if dq <= reach and (best is None or dq < best[0] - 1e-12):
                pt = d.PointOnShape2(1)
                best = (dq, k, np.array([pt.X(), pt.Y(), pt.Z()]))
        if best is None:
            return None
        dists.append(best[0])
        at.append(best[1])
        feet.append(best[2])
    if len(set(at)) < 2:
        return None
    if not _alongside(e, probe, dists, feet, faces[at[1]], tol, faces[mine], faces_at=[faces[k] for k in at]):
        return None
    return max(dists), at[1]


def open_places(sewn: Sewn, *, mm_per_unit: float, reach: float | None = None) -> list[dict]:
    """SURF-8: the places where the surfaces do not meet (OPEN_REACH): [{"sheet", "edges", "length_mm", "gap_mm",
    "at_mm"}], the widest gap first. A place's gap is how far its longest edge lies from the face beside it. One
    place is one gap: the free edges joined end to end, and the two sides of it (face A's edges along face B and
    B's along A) - so an edge the sewing cut into pieces, or a gap seen from both sides, is counted once. Its length
    is the free edge on both sides."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    mm = float(mm_per_unit)
    # `reach` (the shape's units) only for SURF-12's own search when the user's notes allow a wider change; the
    # count the user and the record see is always OPEN_REACH
    reach = OPEN_REACH * sewn.tol if reach is None else float(reach)
    comp = ms._compound(sewn.faces)
    vmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(comp, TopAbs_VERTEX, vmap)
    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(comp, TopAbs_FACE, fmap)
    place = {fmap.FindIndex(f): k for k, f in enumerate(sewn.faces)}
    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(comp, TopAbs_EDGE, TopAbs_FACE, owners)
    sheet_of = {k: i for i, ks in enumerate(sewn.sheets) for k in ks}
    boxes = [ms._box(f) for f in sewn.faces]
    found: list = []
    for i in range(1, owners.Extent() + 1):
        e = TopoDS.Edge_s(owners.FindKey(i))
        if owners.FindFromIndex(i).Extent() != 1 or BRep_Tool.Degenerated_s(e):
            continue
        mine = place[fmap.FindIndex(owners.FindFromIndex(i).First())]
        pts = _edge_points(e)
        probe = [pts[int(round(t * (len(pts) - 1)))] for t in NEAR_SAMPLES]
        best, near = None, -1
        for k, f in enumerate(sewn.faces):
            if k == mine:
                continue
            lo, hi = boxes[k]
            if any(np.any(q < lo - reach) or np.any(q > hi + reach) for q in probe):
                continue
            far, dists, feet = 0.0, [], []
            for q in probe:
                d = BRepExtrema_DistShapeShape(ms._vertex(q), f)
                dq = float(d.Value()) if d.IsDone() and d.NbSolution() else float("inf")
                far = max(far, dq)
                if far > reach:
                    break
                dists.append(dq)
                pt = d.PointOnShape2(1)
                feet.append(np.array([pt.X(), pt.Y(), pt.Z()]))
            if far > reach or not _alongside(e, probe, dists, feet, f, sewn.tol, sewn.faces[mine]):
                continue
            if best is None or far < best:
                best, near = far, k
        if best is None:
            # SURF-8 / SURF-12: AN EDGE STOPPING SHORT OF A SHEET DRAWN IN SEVERAL PATCHES lies along no one face over
            # its whole length, but along the sheet: each probe within reach of its nearest other face, and running
            # alongside it (12 Nov: a wall's foot 3.4 mm short of a stamped panel, across three of its patches, was
            # no open place at all). The face beside it is the one nearest its middle
            got = _along_several(e, mine, probe, sewn.faces, boxes, reach, sewn.tol)
            if got is not None:
                best, near = got
        if best is not None:
            found.append((e, mine, best, near))
    if not found:
        return []
    # the edges joined end to end through the vertices they share are one place
    parent = list(range(len(found)))

    def root(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    at_vertex: dict[int, list[int]] = {}
    # both sides of one gap are one place: the edges of face A lying along face B and those of B lying along A
    by_pair: dict[tuple[int, int], list[int]] = {}
    for n, (_e, k, _g, near) in enumerate(found):
        by_pair.setdefault((min(k, near), max(k, near)), []).append(n)
    for ns in by_pair.values():
        for n in ns[1:]:
            a, b = root(ns[0]), root(n)
            if a != b:
                parent[max(a, b)] = min(a, b)
    for n, (e, _k, _g, _near) in enumerate(found):
        em = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(e, TopAbs_VERTEX, em)
        for j in range(1, em.Extent() + 1):
            at_vertex.setdefault(vmap.FindIndex(em.FindKey(j)), []).append(n)
    for ns in at_vertex.values():
        for n in ns[1:]:
            a, b = root(ns[0]), root(n)
            if a != b:
                parent[max(a, b)] = min(a, b)
    groups: dict[int, list[int]] = {}
    for n in range(len(found)):
        groups.setdefault(root(n), []).append(n)
    out: list[dict] = []
    for ns in groups.values():
        es = [found[n][0] for n in ns]
        mid = _edge_points(max(es, key=ms._length), 3)[1]
        out.append({"_members": [found[n] for n in sorted(ns)],      # (edge, face, gap, face beside): not for JSON
                    "sheet": int(sheet_of[found[ns[0]][1]]), "edges": len(ns),
                    "pairs": sorted({(min(found[n][1], found[n][3]), max(found[n][1], found[n][3])) for n in ns}),
                    "length_mm": _r(sum(ms._length(e) for e in es) * mm),
                    # the place's gap is the gap along its longest edge: a stub at a corner (as long as the gap is
                    # wide) does not make a 3-tolerance seam read as a wider one
                    "gap_mm": _r(found[max(ns, key=lambda n: (ms._length(found[n][0]), -n))][2] * mm),
                    "at_mm": [_r(c * mm) for c in mid]})
    out.sort(key=lambda r: (-float(r["gap_mm"]), list(r["at_mm"])))
    return out


def named(sewn: Sewn, places: list[dict], *, mm_per_unit: float) -> list[dict]:
    """SURF-13: the open places as the mesh can find them - each with its faces by name, pair by pair, and how far
    apart the two sides' free edges are (`apart_mm`: an overlap's gap to the face under it is nil, but its edge lies
    its width from the other's edge): [{"at_mm", "gap_mm", "apart_mm", "length_mm", "faces": [[a, b], ...]}]."""
    from meshpipeline.cad import midsurface as ms

    mm = float(mm_per_unit)
    out = []
    for p in places:
        members = p.get("_members") or []
        apart = 0.0
        for e, k, _g, near in members:
            others = [m[0] for m in members if m[1] == near and m[3] == k]
            if not others:
                continue
            q = _edge_points(e, 3)[1]                         # the middle: a corner's end runs off to the side
            apart = max(apart, min(ms._dist(ms._vertex(q), o) for o in others))
        out.append({"at_mm": list(p["at_mm"]), "gap_mm": p["gap_mm"],
                    "apart_mm": _r(max(apart * mm, float(p["gap_mm"]))), "length_mm": p["length_mm"],
                    "faces": [[sewn.names[a], sewn.names[b]] for a, b in p["pairs"]]})
    return out


def with_places(sheets: list[dict], places: list[dict]) -> list[dict]:
    """The sheets with the open places written in (SURF-8's one definition): unmet_places, unmet_at_mm,
    unmet_widest_mm, unmet_length_mm; unmet_edges stays the count of free edges in them."""
    for i, s in enumerate(sheets):
        mine = [p for p in places if p["sheet"] == i]
        s.update(unmet_places=len(mine), unmet_edges=sum(int(p["edges"]) for p in mine),
                 unmet_at_mm=[p["at_mm"] for p in mine][:MAX_PLACES],
                 unmet_widest_mm=max([float(p["gap_mm"]) for p in mine] + [0.0]),
                 unmet_length_mm=_r(sum(float(p["length_mm"]) for p in mine)))
    return sheets


def has_solid(shape) -> bool:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    return bool(TopExp_Explorer(shape, TopAbs_SOLID).More())


def classify(sheets: list[dict], *, said_shell: bool = False) -> tuple[str, str, float]:
    """SURF-1: (kind, why, the file's open share) from the measured sheets. "closed" when any sheet closes (the
    file's closed shells are read as the solids they bound, MESH-1, as before); "gap" when the openings of the whole
    file span at most GAP_SHARE of its surface and the user's words did not ask for a shell; else "surface"."""
    if not sheets:
        return "empty", "the file holds no faces", 0.0
    area = sum(s["area_mm2"] for s in sheets)
    spanned = sum(s["open_share"] * s["area_mm2"] for s in sheets)
    share = spanned / area if area > 0 else 0.0
    if any(s["closed"] for s in sheets):
        return "closed", "its faces close into a solid", _r(share)
    pct = f"{100 * share:.2g}%"
    if share > GAP_SHARE:
        return "surface", f"it holds no solid and its faces do not close: the open edges span {pct} of the surface", \
            _r(share)
    if said_shell:
        return ("surface", f"its faces do not close (the open edges span {pct} of the surface), and a shell was "
                           "asked for", _r(share))
    return ("gap", f"its faces nearly close: the open edges span {pct} of the surface - a gap in a closed skin "
                   "rather than the edge of a sheet", _r(share))


def read(shape, *, mm_per_unit: float, said_shell: bool = False, stitch: bool = True,
         sew_mm: float | None = None, holes_mm: float | None = None, mend: bool = True,
         element_mm: float | None = None, wall_mm: float | None = None) -> tuple[dict, Sewn | None]:
    """SURF-1: what kind of file this is, with the sheets as measured: (the reading, the sewn faces - None unless
    the reading is "surface" or "gap"). A surface part is then cleaned (SURF-8..10, cad/surface_clean): a face written
    twice is left out, the gaps between neighbouring faces are closed up the sewing ladder, pinholes are filled -
    and the reading says what was done ("cleaned") and measures what is left. `stitch` False: the user said not
    to; `sew_mm`: the stitch tolerance their notes state (None: the rule's); `holes_mm`: the size under which they
    asked for holes to be filled (None: only pinholes no wider than the gaps being closed)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_clean as sc
    from meshpipeline.cad.validity_brep import SEW_TOLERANCE, _bbox

    if has_solid(shape):
        return {"kind": "solid", "why": "the file holds a solid", "sheets": []}, None
    # SURF-9: A FACE WRITTEN TWICE is left out before anything is sewn or judged (sewn in, it pairs its edges with
    # its twin's and the two read as a closed pocket)
    twice: list[int] = []
    try:
        drawn = ms._faces(shape)
        lo, hi = _bbox(shape)
        if len(drawn) > 1 and lo is not None:
            _kept, twice = sc.without_duplicates(drawn, SEW_TOLERANCE * float(np.linalg.norm(hi - lo)))
    except Exception as exc:  # noqa: BLE001 - the file as written
        logger.info("surface: faces written twice were not looked for (%s)", type(exc).__name__)
        twice = []
    out = frozenset(twice)
    sewn = sew(shape, leave_out=out)
    if sewn is None:
        return {"kind": "empty", "why": "the file holds no faces", "sheets": []}, None
    sheets = measure(sewn, mm_per_unit=mm_per_unit)
    kind, why, share = classify(sheets, said_shell=said_shell)
    unmet = sum(int(s["unmet_edges"]) for s in sheets)
    if kind in ("surface", "gap") and unmet:
        # SURF-2: EDGES LEFT LYING ALONG A NEIGHBOUR may be a junction - a rib's foot on the line two plate faces
        # share, three faces at one edge, which the plain sewing leaves apart (it joins two faces at an edge). Sewn
        # again with junctions allowed, and kept when fewer edges are left unmet and nothing closed
        joined = sew(shape, junctions=True, leave_out=out)
        if joined is not None and len(joined.faces) == len(sewn.faces):
            again = measure(joined, mm_per_unit=mm_per_unit)
            if sum(int(s["unmet_edges"]) for s in again) < unmet and not any(s["closed"] for s in again):
                sewn, sheets = joined, again
                unmet = sum(int(s["unmet_edges"]) for s in sheets)
        if unmet:
            # ... and a foot that lands inside a face is joined by scoring that face along it (imprint)
            scored = imprint(sewn)
            if scored is not None:
                again = measure(scored, mm_per_unit=mm_per_unit)
                if sum(int(s["unmet_edges"]) for s in again) < unmet and not any(s["closed"] for s in again):
                    sewn, sheets = scored, again
        kind, why, share = classify(sheets, said_shell=said_shell)
    cleaned: dict | None = None
    if kind == "surface":
        # SURF-8 / SURF-10: THE CLEAN-UP, only once the file is known to be a surface part (what kind of file it is
        # was read above from the file as written: a closed skin with a crack is still a closed skin with a crack)
        cleaned = {"duplicates": len(twice), "duplicate_faces": [f"Face_{k + 1}" for k in twice][:MAX_PLACES]}
        if stitch:
            first = len(open_places(sewn, mm_per_unit=mm_per_unit))
            if mend:
                # SURF-12: THE PLACES WHERE THE SURFACES DO NOT MEET made to meet by changing the surfaces - an edge
                # stopping short extended along its own surface, an overlap trimmed back, a step moved onto its
                # neighbour - one place at a time, each change judged and told. First, so a gap across a face is
                # closed by continuing the face, never papered over by a sewing tolerance; stitching then closes
                # what is left within its own tolerances
                from meshpipeline.cad import surface_mend as smd

                try:
                    # the reach: one element - the user's size, else the size the run will mesh at (SURF-12)
                    size, size_from = (element_mm, "asked") if element_mm else (
                        smd.run_element_mm(sheets, wall_mm), "run")
                    mended, mrec = smd.mend(sewn, mm_per_unit=mm_per_unit, limit_mm=sew_mm, element_mm=size,
                                            element_from=size_from)
                except Exception as exc:  # noqa: BLE001 - the surface as sewn; what is open is said
                    logger.warning("surface: the surfaces were not changed (%s: %s)", type(exc).__name__, str(exc)[:200])
                    mended, mrec = sewn, {"error": type(exc).__name__}
                cleaned["mend"] = mrec
                if mended is not sewn and isinstance(mended, Sewn):
                    sewn = mended
            else:
                cleaned["mend"] = {"off": True}
            try:
                tidy, did = sc.clean(sewn, mm_per_unit=mm_per_unit, limit_mm=sew_mm, holes_mm=holes_mm)
            except Exception as exc:  # noqa: BLE001 - the surface as sewn; what is open is said
                logger.warning("surface: the clean-up failed (%s: %s)", type(exc).__name__, str(exc)[:200])
                tidy, did = sewn, {"refused": "the clean-up could not be run"}
            cleaned.update(did)
            cleaned["places_before"] = first             # before anything was changed or stitched
            if tidy is not sewn and isinstance(tidy, Sewn):
                sewn = tidy
            sheets = measure(sewn, mm_per_unit=mm_per_unit)
        else:
            cleaned.update(stitch="off")
        # SURF-8: WHAT IS LEFT OPEN, by the one definition (open_places), whatever was asked: the record, the words and
        # the guard on a stated tolerance all read this
        places = open_places(sewn, mm_per_unit=mm_per_unit)
        sheets = with_places(sheets, places)
        # with nothing stitched (do not stitch, or the clean-up failed) every open place is one left open
        applied = float(cleaned["applied_mm"]) if cleaned.get("applied_mm") is not None else float("inf")
        within = [p for p in places if float(p["gap_mm"]) <= applied]
        wide = [p for p in places if float(p["gap_mm"]) > applied]
        if cleaned.get("places_before") is not None:
            # the places closed by the whole clean-up (stitching and changing the surfaces): before - after
            cleaned["closed_places"] = max(0, int(cleaned["places_before"]) - len(places))
        try:
            cleaned["places"] = named(sewn, places, mm_per_unit=mm_per_unit)[:MAX_NAMED]
        except Exception as exc:  # noqa: BLE001 - not named: the mesh closes none of them, all are told open
            logger.info("surface: the open places were not named (%s)", type(exc).__name__)
            cleaned["places"] = []
        cleaned.update(open_places=len(places), open_length_mm=_r(sum(float(p["length_mm"]) for p in places)),
                       left_open=len(within), left_widest_mm=max([float(p["gap_mm"]) for p in within] + [0.0]),
                       left_at_mm=[p["at_mm"] for p in within][:MAX_PLACES],
                       wider_gaps=len(wide), wider_widest_mm=max([float(p["gap_mm"]) for p in wide] + [0.0]),
                       wider_narrowest_mm=min([float(p["gap_mm"]) for p in wide] or [0.0]),
                       wider_at_mm=[p["at_mm"] for p in wide][:MAX_PLACES])
    reading = {"kind": kind, "why": why, "faces": len(set(sewn.names)), "sew_tolerance_mm": _r(sewn.tol * mm_per_unit),
               "diag_mm": _r(sewn.diag * mm_per_unit), "open_share": share, "said_shell": bool(said_shell),
               "sheets": sheets}
    if cleaned is not None:
        reading["cleaned"] = cleaned
    return reading, (sewn if kind in ("surface", "gap") else None)


def read_file(path: Path | str, *, mm_per_unit: float, said_shell: bool = False) -> dict:
    """read() on a STEP, IGES or BREP file: the reading alone (no faces kept)."""
    from meshpipeline.cad.scout import read_cad

    return read(read_cad(Path(path)), mm_per_unit=mm_per_unit, said_shell=said_shell)[0]


def _face_width_kind(face, mm: float) -> tuple[float, str]:
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    from meshpipeline.cad import fea_measure as fm
    from meshpipeline.cad import midsurface as ms

    return ms.width_of(face) * mm, fm._kind_word(BRepAdaptor_Surface(face, True).GetType())


def record(reading: dict, sewn: Sewn, *, mm_per_unit: float, walls_mm: dict[str, float],
           given: dict[str, bool] | None = None) -> tuple[dict, object]:
    """SURF-3: the shell record and the surface the shell mesher reads (cad/midsurface's shell.json and
    midsurface.brep), built from the sewn surface itself: every sheet a part and one wall, every CAD face its own
    mid-face (so its own group in every solver file), the sheet's size the square root of its area as for an
    extracted sheet. `walls_mm`: each part's wall (the user's thickness, or the sizing wall when none was given -
    `given` says which). Returns (the record, the compound of the sewn faces in the record's order)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import shell_pairs as sp

    mm = float(mm_per_unit)
    bodies, pairs, roles, mids, census, order, owner = [], [], [], [], [], [], []
    for s, (sheet, ks) in enumerate(zip(reading["sheets"], sewn.sheets, strict=True)):
        part = sheet["part"]
        wall = float(walls_mm[part])
        set_ = bool((given or {}).get(part, True))
        pid = f"P{s + 1}"
        mine: dict[str, dict] = {}
        for k in ks:
            name = sewn.names[k]
            mid = f"Mid_{int(name.rsplit('_', 1)[-1])}"            # the CAD face's own number: Face_7 -> Mid_7
            width, kind = _face_width_kind(sewn.faces[k], mm)
            area = ms._area(sewn.faces[k]) * mm * mm
            order.append(k)
            owner.append(mid)
            if mid in mine:
                # another piece of the same CAD face (a face scored where a rib lands on it, SURF-2)
                mine[mid]["area_mm2"] = _r(mine[mid]["area_mm2"] + area)
                mine[mid]["width_mm"] = max(mine[mid]["width_mm"], _r(width))
                continue
            roles.append({"face": name, "kind": kind, "role": sp.PAIRED, "pairs": [pid], "body": part})
            mine[mid] = {"id": mid, "pair": pid, "body": part, "faces": [name], "thickness_mm": _r(wall),
                         "thickness_set": set_, "shell": True, "area_mm2": _r(area), "width_mm": _r(width),
                         "kind": kind, "extended": [], "fidelity": {"max_error_mm": 0.0, "points": 0}}
        mids += list(mine.values())
        pairs.append({"id": pid, "faces": list(sheet["faces"]), "thickness_mm": _r(wall), "thickness_set": set_,
                      "spread": 0.0, "rays": 0, "even": True, "why": "a surface: the wall is the one given",
                      "sheet": f"S{s + 1}", "size_mm": sheet["size_mm"], "width_mm": None,
                      "ratio": _r(wall / sheet["size_mm"]) if sheet["size_mm"] else None, "shell": True,
                      "mids": list(mine)})
        bodies.append({"body": part, "verdict": "shell", "why": "a surface, meshed as given",
                       "area_mm2": sheet["area_mm2"], "volume_mm3": None, "shell_area_share": 1.0})
        census.append({"body": part, "join": "sewn", "faces": len(sheet["faces"]), "edges": sheet["edges"],
                       "shared_edges": sheet["shared_edges"], "junction_edges": sheet["junction_edges"],
                       "boundary_edges": sheet["free_edges"] - sheet["unmet_edges"],
                       "open_edges": int(sheet.get("unmet_places", sheet["unmet_edges"])),
                       "open_length_mm": sheet.get("unmet_length_mm"), "open_at_mm": sheet["unmet_at_mm"]})
    unmet = sum(c["open_edges"] for c in census)
    notes = [f"The file is a surface ({reading['why']}): it is meshed as a shell on the surface as given (SURF-1)."]
    if unmet:
        notes.append(f"The surfaces do not meet at {unmet} place(s) (free edges lying along another face within "
                     f"{OPEN_REACH * float(reading['sew_tolerance_mm']):.3g} mm): the shell has gaps there (SURF-2, "
                     "SURF-8).")
    c = reading.get("cleaned") or {}
    if c.get("duplicates"):
        notes.append(f"{c['duplicates']} face(s) written twice in the file were left out once (SURF-9).")
    if c.get("closed_edges"):
        notes.append(f"{c['closed_edges']} gap(s) between neighbouring faces were closed, the widest "
                     f"{c['closed_widest_mm']:.3g} mm, within {c['limit_mm']:.3g} mm "
                     f"({'the tolerance the notes state' if c.get('limit_from') == 'notes' else 'the rule'}, SURF-8).")
    m = c.get("mend") or {}
    if m.get("closed"):
        notes.append(f"{m['closed']} place(s) where the surfaces did not meet were made to meet by changing the surfaces "
                     f"({m.get('extended', 0)} extended, {m.get('trimmed', 0)} trimmed, {m.get('moved', 0)} moved), by "
                     f"up to {float(m.get('widest_mm') or 0.0):.3g} mm, within {float(m.get('reach_mm') or 0.0):.3g} mm "
                     "(SURF-12).")
    if c.get("pinholes"):
        notes.append(f"{c['pinholes']} hole(s) up to {c['pinhole_widest_mm']:.3g} mm across were filled with the "
                     "face's own surface (SURF-10).")
    rec = {"version": ms.VERSION, "unit": "mm", "ratio": sp.SHELL_RATIO, "diag_mm": reading["diag_mm"],
           "source": "surface", "surface_model": reading,
           "bodies": bodies, "pairs": pairs, "faces": roles, "mids": mids, "sewn": census,
           "surface_faces": owner,
           "checks": {"fidelity": True, "connected": unmet == 0, "faces_accounted": True, "volume": True},
           "fallback_regions": [], "complete": True, "notes": notes, "seconds": 0.0}
    return rec, ms._compound([sewn.faces[k] for k in order])


def write(rec: dict, surface, out_dir: Path | str) -> None:
    """shell.json and midsurface.brep in `out_dir`, as cad/midsurface writes them."""
    from OCP.BRepTools import BRepTools

    from meshpipeline.cad import midsurface as ms

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    BRepTools.Write_s(surface, str(out / ms.SURFACE_FILE))
    (out / ms.RECORD_FILE).write_text(json.dumps(rec, indent=1, sort_keys=True))


__all__ = ["GAP_SHARE", "NEAR_SEW", "Sewn", "classify", "has_solid", "imprint", "measure", "read", "read_file", "record", "sew",
           "write"]
