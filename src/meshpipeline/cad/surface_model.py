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
#: points read along an edge: for its loop's span, and (the inner ones) for the edges that do not meet
EDGE_POINTS = 16
NEAR_SAMPLES = (0.25, 0.5, 0.75)
#: places kept per sheet
MAX_PLACES = 12


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


def sew(shape, *, junctions: bool = False) -> Sewn | None:
    """SURF-2: the file's faces sewn as the clean check sews them (cad/validity_brep: BRepBuilderAPI_Sewing at
    SEW_TOLERANCE of the diagonal - edges that meet within it become one edge, every other edge stays free) and
    grouped into sheets. With `junctions`, an edge three or more faces meet along (a rib on a plate, a T) is sewn
    into one edge too - the same tolerance, the sewing's own non-manifold mode. None when the file holds no face
    with a size."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

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
    sewing.Add(shape)
    sewing.Perform()
    sewn = sewing.SewedShape()
    if sewn is None or sewn.IsNull():
        return None
    index = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(sewn, TopAbs_FACE, index)
    faces, names = [], []
    seen: set[int] = set()
    for k, f in enumerate(before):
        g = sewing.ModifiedSubShape(f)
        i = index.FindIndex(g) if g is not None and not g.IsNull() else 0
        if i <= 0 or i in seen:
            continue                              # a face the sewing dropped (degenerate), or met twice
        seen.add(i)
        faces.append(TopoDS.Face_s(index.FindKey(i)))
        names.append(f"Face_{k + 1}")
    if not faces:
        return None
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
    return Sewn(faces, names, _components(len(faces), links), tol, diag)


def _unmet(free: list, own: list[int], faces: list, boxes: list, reach: float) -> list[np.ndarray | None]:
    """SURF-2: for each free edge, its middle when it lies along another face (every sample point within `reach` of
    a face that is not the edge's own: a seam the sewing did not close, a rib that does not reach its plate), else
    None."""
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
            near = True
            for p in probe:
                d = BRepExtrema_DistShapeShape(ms._vertex(p), f)
                if not d.IsDone() or d.Value() > reach:
                    near = False
                    break
            if near:
                found = probe[1]
                break
        out.append(found)
    return out


def measure(sewn: Sewn, *, mm_per_unit: float) -> list[dict]:
    """Each sheet of the sewn file, measured (SURF-1, SURF-2): its area and size, its free edges chained into loops
    with the area each spans, its open share, and the edges that do not meet their neighbour."""
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
    reach = NEAR_SEW * sewn.tol
    areas = [sum(ms._area(sewn.faces[k]) for k in ks) for ks in sewn.sheets]
    out = []
    for s, ks in enumerate(sewn.sheets):
        area = areas[s]
        free = free_of.get(s, [])
        unmet = _unmet(free, own_of.get(s, []), sewn.faces, boxes, reach) if free else []
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
        out.append({"part": f"Part_{s + 1}", "faces": [sewn.names[k] for k in ks], "area_mm2": _r(area_mm2),
                    "size_mm": _r(math.sqrt(area_mm2)), "edges": edges_in.get(s, 0), "shared_edges": shared.get(s, 0),
                    "junction_edges": junction.get(s, 0), "free_edges": len(free),
                    "free_length_mm": _r(sum(ms._length(e) for e in free) * mm),
                    "loops": rows[:MAX_PLACES], "loop_count": len(rows),
                    "open_share": _r(spanned / area_mm2) if area_mm2 > 0 else 0.0,
                    "unmet_edges": len(places), "unmet_at_mm": [[_r(c * mm) for c in p] for p in places[:MAX_PLACES]],
                    "closed": not free})
    return out


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


def read(shape, *, mm_per_unit: float, said_shell: bool = False) -> tuple[dict, Sewn | None]:
    """SURF-1: what kind of file this is, with the sheets as measured: (the reading, the sewn faces - None unless
    the reading is "surface" or "gap")."""
    if has_solid(shape):
        return {"kind": "solid", "why": "the file holds a solid", "sheets": []}, None
    sewn = sew(shape)
    if sewn is None:
        return {"kind": "empty", "why": "the file holds no faces", "sheets": []}, None
    sheets = measure(sewn, mm_per_unit=mm_per_unit)
    kind, why, share = classify(sheets, said_shell=said_shell)
    unmet = sum(int(s["unmet_edges"]) for s in sheets)
    if kind in ("surface", "gap") and unmet:
        # SURF-2: EDGES LEFT LYING ALONG A NEIGHBOUR may be a junction - a rib's foot on the line two plate faces
        # share, three faces at one edge, which the plain sewing leaves apart (it joins two faces at an edge). Sewn
        # again with junctions allowed, and kept when fewer edges are left unmet and nothing closed
        joined = sew(shape, junctions=True)
        if joined is not None and len(joined.faces) == len(sewn.faces):
            again = measure(joined, mm_per_unit=mm_per_unit)
            if sum(int(s["unmet_edges"]) for s in again) < unmet and not any(s["closed"] for s in again):
                sewn, sheets = joined, again
                kind, why, share = classify(sheets, said_shell=said_shell)
    reading = {"kind": kind, "why": why, "faces": len(sewn.faces), "sew_tolerance_mm": _r(sewn.tol * mm_per_unit),
               "diag_mm": _r(sewn.diag * mm_per_unit), "open_share": share, "said_shell": bool(said_shell),
               "sheets": sheets}
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
    place = {n: k for k, n in enumerate(sewn.names)}
    bodies, pairs, roles, mids, census, order = [], [], [], [], [], []
    for s, sheet in enumerate(reading["sheets"]):
        part = sheet["part"]
        wall = float(walls_mm[part])
        set_ = bool((given or {}).get(part, True))
        pid = f"P{s + 1}"
        mine = []
        for name in sheet["faces"]:
            k = place[name]
            width, kind = _face_width_kind(sewn.faces[k], mm)
            mid = f"Mid_{k + 1}"
            mine.append(mid)
            order.append(k)
            roles.append({"face": name, "kind": kind, "role": sp.PAIRED, "pairs": [pid], "body": part})
            mids.append({"id": mid, "pair": pid, "body": part, "faces": [name], "thickness_mm": _r(wall),
                         "thickness_set": set_, "shell": True, "area_mm2": _r(ms._area(sewn.faces[k]) * mm * mm),
                         "width_mm": _r(width), "kind": kind, "extended": [],
                         "fidelity": {"max_error_mm": 0.0, "points": 0}})
        pairs.append({"id": pid, "faces": list(sheet["faces"]), "thickness_mm": _r(wall), "thickness_set": set_,
                      "spread": 0.0, "rays": 0, "even": True, "why": "a surface: the wall is the one given",
                      "sheet": f"S{s + 1}", "size_mm": sheet["size_mm"], "width_mm": None,
                      "ratio": _r(wall / sheet["size_mm"]) if sheet["size_mm"] else None, "shell": True, "mids": mine})
        bodies.append({"body": part, "verdict": "shell", "why": "a surface, meshed as given",
                       "area_mm2": sheet["area_mm2"], "volume_mm3": None, "shell_area_share": 1.0})
        census.append({"body": part, "join": "sewn", "faces": len(sheet["faces"]), "edges": sheet["edges"],
                       "shared_edges": sheet["shared_edges"], "junction_edges": sheet["junction_edges"],
                       "boundary_edges": sheet["free_edges"] - sheet["unmet_edges"],
                       "open_edges": sheet["unmet_edges"], "open_at_mm": sheet["unmet_at_mm"]})
    unmet = sum(c["open_edges"] for c in census)
    notes = [f"The file is a surface ({reading['why']}): it is meshed as a shell on the surface as given (SURF-1)."]
    if unmet:
        notes.append(f"{unmet} edge(s) lie along a neighbouring face without meeting it within "
                     f"{reading['sew_tolerance_mm']:.3g} mm: the shell has gaps there (SURF-2).")
    rec = {"version": ms.VERSION, "unit": "mm", "ratio": sp.SHELL_RATIO, "diag_mm": reading["diag_mm"],
           "source": "surface", "surface_model": reading,
           "bodies": bodies, "pairs": pairs, "faces": roles, "mids": mids, "sewn": census,
           "surface_faces": [f"Mid_{k + 1}" for k in order],
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


__all__ = ["GAP_SHARE", "NEAR_SEW", "Sewn", "classify", "has_solid", "measure", "read", "read_file", "record", "sew",
           "write"]
