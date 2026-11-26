# Responsibility: the kernel half of v5's open_shell_volume detectors: MEASURE a BREP's shells - its units (solids,
#                 free shells, loose faces), free edges and the GAP each one is from a partner it could be sewn to,
#                 non-manifold edges, signed solid volumes, closed free-edge loops, and the wire gaps of named faces.
#                 Never decides; `open_shell.py` does.
# Owns: `v5_shell_facts` (registered kernel fn) and the measurements it is built from.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. Indices are the BREP's own TopExp map
#             indices (`ws_kernel._maps`), i.e. what `ws.indices` holds, so the parent translates them exactly.
"""Facts for the open_shell_volume class.

- units: every solid (`S<k>`, its signed volume, free and non-manifold edge counts), every free shell (`H<k>`) and
  the loose faces (`L`), each with its face count, bounding-box diagonal and whether its faces could ever enclose a
  volume (the closable test of `sites_kernel.volume_possibility`);
- free edges: each with its unit, length, and its GAP - the largest of the distances, at three interior samples, to
  the nearest free edge of ANOTHER face; a free edge whose gap is over `reach` (or over a quarter of its own length:
  then the "partner" is the next edge round a corner, not across a seam) is UNPAIRED: no sewing can close it;
- loops: the closed free-edge wires (edges, span, how many of their edges are paired);
- non-manifold edges (more than two faces) with their face count;
- per named face (`faces`): the largest 3D gap between consecutive edges of its wires (gmsh's open_loop).

Bounded by `CALL_BUDGET_S` (`truncated` says so) and `MAX_FREE_EDGES` (gaps measured on the first ones only).
"""

from __future__ import annotations

import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register

CALL_BUDGET_S = 60.0
#: free edges whose gap is measured (the rest are counted)
MAX_FREE_EDGES = 1500
MAX_LOOPS = 32
MAX_NON_MANIFOLD = 64
#: a partner further than this share of the part diagonal is no partner (sewing that far is not a repair)
REACH_OF_DIAGONAL = 0.01
#: a partner further than this share of the edge's own length is the next edge round a corner, not across a seam
REACH_OF_LENGTH = 0.25
SAMPLES = (0.25, 0.5, 0.75)


def _box_of(shape: Any) -> tuple[float, ...] | None:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return None
    return tuple(box.Get())


def _diag(box: tuple[float, ...] | None) -> float:
    return 0.0 if box is None else math.dist(box[:3], box[3:])


def _children(shape: Any, kind: Any) -> list[Any]:
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    indexed = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, indexed)
    return [indexed.FindKey(i) for i in range(1, indexed.Extent() + 1)]


def _volume(shape: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass())


def _owners(listed: Any, faces: Any) -> list[int]:
    """The face indices in a TopTools_ListOfShape, one per use. Python iteration over an OCP list costs milliseconds
    per item (measured: 1.2 s for 204 edges), so the usual sizes (1 and 2) are read by First/Last."""
    size = listed.Size()
    if size == 0:
        return []
    if size == 1:
        return [faces.FindIndex(listed.First())]
    if size == 2:
        return [faces.FindIndex(listed.First()), faces.FindIndex(listed.Last())]
    return [faces.FindIndex(f) for f in listed]


def _closable(faces: list[int], uses: dict[int, list[int]]) -> bool:
    """`sites_kernel.volume_possibility`'s test over these faces: drop any face with an edge used fewer than twice by
    the faces left; something left = a volume is possible."""
    alive = set(faces)
    while True:
        count: dict[int, int] = {}
        for f in alive:
            for e in uses[f]:
                count[e] = count.get(e, 0) + 1
        drop = {f for f in alive if any(count[e] < 2 for e in uses[f])}
        if not drop:
            return bool(alive)
        alive -= drop


def _samples(edge: Any) -> list[Any]:
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    curve = BRepAdaptor_Curve(edge)
    t0, t1 = curve.FirstParameter(), curve.LastParameter()
    return [curve.Value(t0 + (t1 - t0) * s) for s in SAMPLES]


def _length(edge: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    return float(props.Mass())


#: points per free edge in the candidate polylines, and how many candidates are measured exactly per sample
POLYLINE_POINTS = 17
CANDIDATES = 3


class _Polylines:
    """Every free edge as a polyline, for picking a sample's nearest-edge CANDIDATES with numpy; the exact distance is
    then measured to those few only. `sag` is the largest polyline-to-curve deviation (sampled at segment middles)."""

    def __init__(self, items: list[tuple[int, int, Any]]) -> None:
        import numpy as np
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.TopoDS import TopoDS

        starts, ends, owners, faces = [], [], [], []
        sag = 0.0
        for e, f, edge in items:
            curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
            t0, t1 = curve.FirstParameter(), curve.LastParameter()
            pts = [curve.Value(t0 + (t1 - t0) * k / (POLYLINE_POINTS - 1)) for k in range(POLYLINE_POINTS)]
            xyz = [(p.X(), p.Y(), p.Z()) for p in pts]
            for k in range(POLYLINE_POINTS - 1):
                mid = curve.Value(t0 + (t1 - t0) * (k + 0.5) / (POLYLINE_POINTS - 1))
                chord = [(a + b) / 2 for a, b in zip(xyz[k], xyz[k + 1], strict=True)]
                sag = max(sag, math.dist(chord, (mid.X(), mid.Y(), mid.Z())))
                starts.append(xyz[k])
                ends.append(xyz[k + 1])
                owners.append(e)
                faces.append(f)
        self.a = np.asarray(starts, dtype=float).reshape(-1, 3)
        self.d = np.asarray(ends, dtype=float).reshape(-1, 3) - self.a
        self.dd = np.maximum((self.d * self.d).sum(axis=1), 1e-300)
        self.owner = np.asarray(owners, dtype=int)
        self.face = np.asarray(faces, dtype=int)
        self.sag = sag

    def nearest(self, p: tuple[float, float, float], *, exclude_face: int, reach: float) -> list[int]:
        import numpy as np

        if not len(self.owner):
            return []
        q = np.asarray(p, dtype=float)
        t = np.clip(((q - self.a) * self.d).sum(axis=1) / self.dd, 0.0, 1.0)
        dist = np.linalg.norm(self.a + t[:, None] * self.d - q, axis=1)
        dist[self.face == exclude_face] = np.inf
        out: list[int] = []
        for k in np.argsort(dist):
            if dist[k] > reach or len(out) >= CANDIDATES:
                break
            if int(self.owner[k]) not in out:
                out.append(int(self.owner[k]))
        return out


def _wire_gap(face: Any) -> float:
    """The largest 3D distance between the end of one edge and the start of the next, over the face's wires."""
    from OCP.ShapeAnalysis import ShapeAnalysis_Wire
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    worst = 0.0
    for wire in _children(face, TopAbs_WIRE):
        analysis = ShapeAnalysis_Wire(TopoDS.Wire_s(wire), TopoDS.Face_s(face), 1e-7)
        analysis.CheckGaps3d()
        worst = max(worst, float(analysis.MaxDistance3d()))
    return worst


@register("v5_shell_facts")
def v5_shell_facts(payload: dict) -> dict:
    """payload: `brep_path`, optional `budget_s`, `faces` (1-based indices whose wire gaps to measure)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import (
        TopTools_HSequenceOfShape,
        TopTools_IndexedDataMapOfShapeListOfShape,
        TopTools_IndexedMapOfShape,
    )

    from meshpipeline.cad.repair_v4.ws_kernel import _maps, read_brep

    started = time.monotonic()
    budget = float(payload.get("budget_s") or CALL_BUDGET_S)
    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    faces, edges = maps["face"], maps["edge"]
    whole = _box_of(shape)
    diag = _diag(whole)
    reach = REACH_OF_DIAGONAL * diag

    # units: solids, free shells, loose faces; every face's unit
    unit_of: dict[int, str] = {}
    units: list[dict] = []
    for k, solid in enumerate(_children(shape, TopAbs_SOLID), start=1):
        mine = [faces.FindIndex(f) for f in _children(solid, TopAbs_FACE)]
        for f in mine:
            unit_of.setdefault(f, f"S{k}")
        units.append({"name": f"S{k}", "kind": "solid", "index": maps["solid"].FindIndex(solid),
                      "faces": mine, "volume": _volume(solid), "diag_mm": _diag(_box_of(solid))})
    shells = TopTools_IndexedMapOfShape()
    explorer = TopExp_Explorer(shape, TopAbs_SHELL, TopAbs_SOLID)
    while explorer.More():
        shells.Add(explorer.Current())
        explorer.Next()
    for k in range(1, shells.Extent() + 1):
        shell = shells.FindKey(k)
        mine = [faces.FindIndex(f) for f in _children(shell, TopAbs_FACE)]
        for f in mine:
            unit_of.setdefault(f, f"H{k}")
        units.append({"name": f"H{k}", "kind": "shell", "faces": mine, "diag_mm": _diag(_box_of(shell))})
    loose = [i for i in range(1, faces.Extent() + 1) if i not in unit_of]
    if loose:
        for f in loose:
            unit_of[f] = "L"
        units.append({"name": "L", "kind": "loose", "faces": loose, "diag_mm": diag})

    # edge -> faces over the whole shape; per-face edge uses for the closable test
    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    uses: dict[int, list[int]] = {i: [] for i in range(1, faces.Extent() + 1)}
    free: list[tuple[int, int]] = []          # (edge index, face index)
    non_manifold: list[dict] = []
    for i in range(1, ancestors.Extent() + 1):
        edge = TopoDS.Edge_s(ancestors.FindKey(i))
        if BRep_Tool.Degenerated_s(edge):
            continue
        index = edges.FindIndex(edge)
        owners = _owners(ancestors.FindFromIndex(i), faces)
        # every USE counts (a seam lists its one face twice and is not free); owners are the distinct faces
        for f in owners:
            uses[f].append(index)
        size = ancestors.FindFromIndex(i).Size()
        if size == 2 and len(set(owners)) == 1:
            continue
        distinct = sorted(set(owners))
        if size == 1:
            free.append((index, distinct[0]))
        elif len(distinct) > 2 and len(non_manifold) < MAX_NON_MANIFOLD:
            non_manifold.append({"edge": index, "faces": distinct})
    for unit in units:
        unit["closable"] = _closable(unit["faces"], uses)
        unit["free_edges"] = sum(1 for _, f in free if unit_of.get(f) == unit["name"])
        unit["non_manifold"] = sum(1 for n in non_manifold if unit_of.get(n["faces"][0]) == unit["name"])
        unit["n_faces"] = len(unit.pop("faces"))

    # the gap of every free edge to its nearest free partner on another face
    truncated = False
    polyline = _Polylines([(e, f, edges.FindKey(e)) for e, f in free[:MAX_FREE_EDGES]])
    gaps: list[dict] = []
    for e, f in free[:MAX_FREE_EDGES]:
        if time.monotonic() - started > budget:
            truncated = True
            break
        edge = TopoDS.Edge_s(edges.FindKey(e))
        length = _length(edge)
        limit = min(reach, REACH_OF_LENGTH * length) if length > 0 else reach
        worst, partner = 0.0, None
        for p in _samples(edge):
            best, best_j = math.inf, None
            vertex = BRepBuilderAPI_MakeVertex(p).Vertex()
            # the polylines pick the candidates; the exact distance is measured to those only
            for j in polyline.nearest((p.X(), p.Y(), p.Z()), exclude_face=f, reach=limit + polyline.sag):
                dist = BRepExtrema_DistShapeShape(vertex, edges.FindKey(j))
                if dist.IsDone() and dist.Value() < best:
                    best, best_j = float(dist.Value()), j
            worst = max(worst, best)
            partner = partner if partner is not None else best_j
            if worst > limit:
                break
        paired = worst <= limit
        gaps.append({"edge": e, "face": f, "unit": unit_of.get(f), "length_mm": length,
                     "gap_mm": worst if paired else None, "partner": partner if paired else None})
    if len(free) > MAX_FREE_EDGES:
        truncated = True
    paired_edges = {g["edge"] for g in gaps if g["gap_mm"] is not None}

    # closed free-edge loops (the edges `fill_loop` takes)
    sequence = TopTools_HSequenceOfShape()
    for e, _ in free:
        sequence.Append(edges.FindKey(e))
    wires = TopTools_HSequenceOfShape()
    if sequence.Length():
        ShapeAnalysis_FreeBounds.ConnectEdgesToWires_s(sequence, 1e-7, True, wires)
    loops: list[dict] = []
    open_wires = 0
    for w in range(1, wires.Length() + 1):
        wire = wires.Value(w)
        if not wire.Closed():
            open_wires += 1
            continue
        mine = sorted({edges.FindIndex(TopoDS.Edge_s(x)) for x in _children(wire, TopAbs_EDGE)} - {0})
        if mine:
            loops.append({"edges": mine, "span_mm": _diag(_box_of(wire)),
                          "paired": sum(1 for x in mine if x in paired_edges),
                          "units": sorted({unit_of.get(f, "?") for e2, f in free if e2 in mine})})
    loops.sort(key=lambda lp: lp["span_mm"])

    wire_gaps = []
    for f in payload.get("faces") or []:
        if 1 <= int(f) <= faces.Extent():
            wire_gaps.append({"face": int(f), "gap_mm": _wire_gap(faces.FindKey(int(f)))})

    return {"diag_mm": diag, "reach_mm": reach, "units": units, "free_edges": len(free), "gaps": gaps,
            "non_manifold": non_manifold, "loops": loops[:MAX_LOOPS], "loops_total": len(loops),
            "open_wires": open_wires, "wire_gaps": wire_gaps, "truncated": truncated,
            "seconds": round(time.monotonic() - started, 3)}
