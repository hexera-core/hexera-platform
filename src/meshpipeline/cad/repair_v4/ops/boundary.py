# Responsibility: CRS v5 round 2 boundary repairs for invalid_brep_tolerance and closed_periodic_face:
#                 `rebuild_boundary_edge` (a face wire MISSING an edge - a 3D gap between two consecutive edges - gets
#                 that edge back, one edge shared by every face whose wire has the same gap) and `rebuild_seam` (a
#                 seam edge whose 3D curve is off its surface gets the surface's own iso-curve as its 3D curve).
# Owns: `OPS` (both ops, found by the ops package), the kernel fns `v5_rebuild_boundary_edge` / `v5_rebuild_seam` and
#       the measurements they share with the detector (`wire_gaps`, `seam_deviation`).
# Boundaries: the parent half imports no OCCT. History and the no-new-invalid check are `edges._finish`'s. Targets
#             are the faces the brep_faults detector aimed at (`missing_edge` / `bad_seam` faults).
"""Rebuild a missing boundary edge; rebuild a seam from its surface.

MISSING EDGE (measured, v5 step 5 + round 2): a degenerated edge flagged on a real edge does not survive the STEP
round trip - the writer drops it, and both faces that shared it read back with a wire whose two consecutive edges
end at DIFFERENT vertices (BRepCheck: UnorientableShape; the meshers: import_failed). Nothing on v4's menu adds an
edge: ShapeFix_Face/ShapeFix_Wire close such a gap per face (FixLacking) or by moving vertices (FixConnected), which
leaves the two faces unshared. This op builds ONE edge between the two gap vertices - the straight line when it lies
on every face that lacks it (within the fidelity bound), else the faces' intersection curve through both vertices -
inserts it into each lacking wire (ShapeFix_Wire FixReorder + FixEdgeCurves give it its pcurves), and so restores
the shared boundary. On 8 of 10 injected pole cases (5 sources x 2 seeds) the result reads back from STEP with no
BRepCheck fault and no free edge (local probe, round 2); the other 2 are one face with two gaps and no partner.

BAD SEAM: a seam (an edge closed on its face) whose 3D curve is not the surface's iso at the seam's pcurve - two
exporters disagreeing on where the cylinder is cut (`seam_variant`, a 10.8 mm seam tolerance on 00607178). The 3D
curve is replaced, in place, by `surface.UIso(u)` (or VIso) over the pcurve's range, and SameParameter re-run.
"""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops.edges import (
    KernelDeclined,
    _declines,
    _finish,
    _indexed,
    _run,
    _split,
    fidelity_bound,
)
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

#: samples along a candidate edge when checking it lies on a face, and along a seam when measuring its deviation
_SAMPLES = 9
#: a gap counts only when it is wider than this many times the two vertices' tolerances (a closed wire's vertices
#: are the SAME vertex; distinct vertices within tolerance are connected for every reader)
GAP_TOLS = 2.0
#: a seam is bad when its 3D curve is farther than this from the surface iso (provisional: 10x layer 1's tightest
#: tolerance scale; the injected seam_variant is 10.8 mm off, a clean seam is ~1e-7)
SEAM_DEV_MM = 1e-3


# ---------------------------------------------------------------- kernel half (runs in a worker)

def wire_gaps(face: Any) -> list[tuple[Any, int, Any, Any, float]]:
    """(wire, i, open end, the open end it pairs with, gap mm) for every 3D gap in the face's wires. A closed wire
    uses every vertex an EVEN number of times (edge ends, a seam's two uses included), whatever order its edges are
    stored in; a vertex used an odd number of times is an OPEN END. Open ends are paired nearest-first, and a pair
    farther apart than `GAP_TOLS` x their tolerances is a gap (a missing edge). `i` numbers the gaps per wire."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_WIRE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out = []
    ex = TopExp_Explorer(face, TopAbs_WIRE)
    while ex.More():
        wire = TopoDS.Wire_s(ex.Current())
        ex.Next()
        uses: list[list[Any]] = []          # [vertex, count]
        ee = TopExp_Explorer(wire, TopAbs_EDGE)
        while ee.More():
            edge = TopoDS.Edge_s(ee.Current())
            ee.Next()
            for v in (TopExp.FirstVertex_s(edge), TopExp.LastVertex_s(edge)):
                if v.IsNull():
                    continue
                row = next((r for r in uses if r[0].IsSame(v)), None)
                if row is None:
                    uses.append([v, 1])
                else:
                    row[1] += 1
        ends = [v for v, n in uses if n % 2]
        i = 0
        while len(ends) >= 2:
            v1 = ends.pop(0)
            p1 = BRep_Tool.Pnt_s(v1)
            j = min(range(len(ends)), key=lambda k: p1.Distance(BRep_Tool.Pnt_s(ends[k])))
            v2 = ends.pop(j)
            gap = p1.Distance(BRep_Tool.Pnt_s(v2))
            if gap > GAP_TOLS * (BRep_Tool.Tolerance_s(v1) + BRep_Tool.Tolerance_s(v2)):
                i += 1
                out.append((wire, i, v1, v2, gap))
    return out


def seam_deviation(face: Any, edge: Any) -> float | None:
    """Largest distance between a seam's 3D curve and the face's surface at the seam's pcurve, or between the seam's
    curve (and pcurve) ends and its own VERTICES (None: unmeasurable). Measured on the VM image (round 2): the STEP
    reader re-projects a turned seam's pcurves onto its turned 3D curve, so curve and pcurve agree and the fault is
    left between the seam and its vertices (the boundary circles still end at the old angle)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopExp import TopExp

    try:
        surface = BRep_Tool.Surface_s(face)
        pcurve = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
        if pcurve is None:
            return None
        a, b = BRep_Tool.Range_s(edge, face)
        curve = BRepAdaptor_Curve(edge)
        c0, c1 = curve.FirstParameter(), curve.LastParameter()
        worst = 0.0
        for j in range(_SAMPLES):
            t = j / (_SAMPLES - 1)
            uv = pcurve.Value(a + (b - a) * t)
            worst = max(worst, curve.Value(c0 + (c1 - c0) * t).Distance(surface.Value(uv.X(), uv.Y())))
        v0, v1 = TopExp.FirstVertex_s(edge, False), TopExp.LastVertex_s(edge, False)
        for v, t in ((v0, c0), (v1, c1)):
            if not v.IsNull():
                worst = max(worst, BRep_Tool.Pnt_s(v).Distance(curve.Value(t)))
        return worst
    except Exception:  # noqa: BLE001 - an unmeasurable seam is not a fault
        return None


def _iso_through_vertices(face: Any, edge: Any) -> tuple[Any, float, float, float] | None:
    """(iso curve, s0, s1, du) - the surface iso through the seam's two vertices and how far (in the periodic
    parameter) it lies from the seam's current pcurve; None when the vertices do not share one iso."""
    from OCP.BRep import BRep_Tool
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopExp import TopExp

    surface = BRep_Tool.Surface_s(face)
    pcurve = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
    a, b = BRep_Tool.Range_s(edge, face)
    p0, p1 = pcurve.Value(a), pcurve.Value(b)
    v0, v1 = TopExp.FirstVertex_s(edge, False), TopExp.LastVertex_s(edge, False)
    if v0.IsNull() or v1.IsNull():
        return None
    sas = ShapeAnalysis_Surface(surface)
    q0 = sas.ValueOfUV(BRep_Tool.Pnt_s(v0), 1e-7)
    q1 = sas.ValueOfUV(BRep_Tool.Pnt_s(v1), 1e-7)
    tol = 1e-6 * max(1.0, abs(p0.X()) + abs(p0.Y()))
    if abs(p0.X() - p1.X()) <= 1e-9:                 # a V-direction seam at u = const
        period = surface.UPeriod() if surface.IsUPeriodic() else None
        u = q0.X()
        if abs(_wrap(q1.X() - u, period)) > max(tol, 1e-6):
            return None
        du = _wrap(u - p0.X(), period)
        return surface.UIso(p0.X() + du), q0.Y(), q1.Y(), du
    if abs(p0.Y() - p1.Y()) <= 1e-9:
        period = surface.VPeriod() if surface.IsVPeriodic() else None
        v = q0.Y()
        if abs(_wrap(q1.Y() - v, period)) > max(tol, 1e-6):
            return None
        dv = _wrap(v - p0.Y(), period)
        return surface.VIso(p0.Y() + dv), q0.X(), q1.X(), dv
    return None


def _wrap(d: float, period: float | None) -> float:
    """`d` brought into (-period/2, period/2] for a periodic parameter."""
    if not period:
        return d
    d = d % period
    return d - period if d > period / 2 else d


def seams_of(face: Any) -> list[Any]:
    """The distinct seam edges of a face (closed on it, not degenerated)."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out: list[Any] = []
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    while ex.More():
        edge = TopoDS.Edge_s(ex.Current())
        ex.Next()
        if BRep_Tool.IsClosed_s(edge, face) and not BRep_Tool.Degenerated_s(edge) and \
                not any(edge.IsSame(s) for s in out):
            out.append(edge)
    return out


def _on_face(curve: Any, a: float, b: float, face: Any, bound: float) -> bool:
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf

    surface = BRep_Tool.Surface_s(face)
    for j in range(_SAMPLES):
        proj = GeomAPI_ProjectPointOnSurf(curve.Value(a + (b - a) * j / (_SAMPLES - 1)), surface)
        if proj.NbPoints() == 0 or proj.LowerDistance() > bound:
            return False
    return True


def _new_edge(v1: Any, v2: Any, faces: list[Any], bound: float) -> tuple[Any, str]:
    """The edge v1 -> v2 lying on every face of `faces`: the line, else the first two faces' intersection curve."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.GC import GC_MakeSegment
    from OCP.GeomAPI import GeomAPI_IntSS, GeomAPI_ProjectPointOnCurve

    p1, p2 = BRep_Tool.Pnt_s(v1), BRep_Tool.Pnt_s(v2)
    line = GC_MakeSegment(p1, p2).Value()
    if all(_on_face(line, line.FirstParameter(), line.LastParameter(), f, bound) for f in faces):
        return BRepBuilderAPI_MakeEdge(v1, v2).Edge(), "line"
    if len(faces) >= 2:
        inter = GeomAPI_IntSS(BRep_Tool.Surface_s(faces[0]), BRep_Tool.Surface_s(faces[1]), 1e-7)
        if inter.IsDone():
            for li in range(1, inter.NbLines() + 1):
                curve = inter.Line(li)
                a, b = GeomAPI_ProjectPointOnCurve(p1, curve), GeomAPI_ProjectPointOnCurve(p2, curve)
                if a.NbPoints() == 0 or b.NbPoints() == 0 or max(a.LowerDistance(), b.LowerDistance()) > bound:
                    continue
                u1, u2 = a.LowerDistanceParameter(), b.LowerDistanceParameter()
                if u1 > u2:
                    curve, u1, u2 = curve.Reversed(), curve.ReversedParameter(u1), curve.ReversedParameter(u2)
                if u2 - u1 <= 0 or not all(_on_face(curve, u1, u2, f, bound) for f in faces):
                    continue
                made = BRepBuilderAPI_MakeEdge(curve, v1, v2, u1, u2)
                if made.IsDone():
                    return made.Edge(), "intersection"
    raise KernelDeclined("no_edge", f"no line or intersection curve between the gap's vertices lies on the "
                                    f"{len(faces)} face(s) within {bound:.3g} mm")


@register_kernel_fn("v5_rebuild_boundary_edge")
@_declines
def _rebuild_boundary_edge(payload: dict) -> dict:
    """Every gap in the target faces' wires closed by ONE edge per vertex pair, shared by every face (target or not)
    whose wire has the same gap."""
    from OCP.ShapeBuild import ShapeBuild_ReShape
    from OCP.ShapeExtend import ShapeExtend_WireData
    from OCP.ShapeFix import ShapeFix_Wire
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4.ws_kernel import part_diagonal, read_brep

    start = time.perf_counter()
    old, base = read_brep(payload["brep_path"]), read_brep(payload["brep_path"])
    bound = fidelity_bound(part_diagonal(payload, base))
    fmap = _indexed(base, "face")
    targets = set(payload["targets"]["face"])
    # every gap of the part, grouped by its (unordered) vertex pair: a missing edge is missing from BOTH its faces
    groups: list[dict] = []
    for i in range(1, fmap.Extent() + 1):
        face = TopoDS.Face_s(fmap.FindKey(i))
        for wire, k, v1, v2, gap in wire_gaps(face):
            group = next((g for g in groups if (g["v1"].IsSame(v1) and g["v2"].IsSame(v2))
                          or (g["v1"].IsSame(v2) and g["v2"].IsSame(v1))), None)
            if group is None:
                group = {"v1": v1, "v2": v2, "gap": gap, "at": []}
                groups.append(group)
            group["at"].append((i, face, wire, k))
    groups = [g for g in groups if any(i in targets for i, *_ in g["at"])]
    if not groups:
        raise KernelDeclined("no_change", f"no wire gap on face(s) {sorted(targets)}")
    ctx = ShapeBuild_ReShape()
    # a wire with two gaps gets both edges: collect the insertions per wire, then rebuild each wire once
    per_wire: dict[int, dict] = {}
    made = []
    for g in groups:
        faces = [f for _i, f, _w, _k in g["at"]]
        edge, how = _new_edge(g["v1"], g["v2"], faces, bound)
        made.append({"faces": sorted({i for i, *_ in g["at"]}), "gap_mm": g["gap"], "curve": how})
        for i, face, wire, k in g["at"]:
            slot = per_wire.setdefault(id(wire), {"face": face, "wire": wire, "add": [], "index": i})
            slot["add"].append((k, edge))
    for slot in per_wire.values():
        data = ShapeExtend_WireData(slot["wire"])
        for _k, edge in slot["add"]:
            data.Add(edge)                  # FixReorder puts it between the two open ends it joins
        fixer = ShapeFix_Wire(data.Wire(), slot["face"], 1e-7)
        fixer.FixReorder()
        fixer.FixEdgeCurves()
        ctx.Replace(slot["wire"], fixer.Wire())
    result = ctx.Apply(base)
    params = {"faces": sorted(targets), "edges_added": made, "fidelity_bound": bound}
    return _finish(payload, "rebuild_boundary_edge", params, old, base, result, ctx.History(), set(), start)


@register_kernel_fn("v5_rebuild_seam")
@_declines
def _rebuild_seam(payload: dict) -> dict:
    """Every bad seam on the target faces gets the surface's own iso-curve as its 3D curve, in place."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.Geom import Geom_TrimmedCurve
    from OCP.gp import gp_Vec2d
    from OCP.ShapeFix import ShapeFix_Edge
    from OCP.TopAbs import TopAbs_FORWARD, TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4.ws_kernel import read_brep

    start = time.perf_counter()
    old, base = read_brep(payload["brep_path"]), read_brep(payload["brep_path"])
    fmap, emap = _indexed(base, "face"), _indexed(base, "edge")
    builder, force, fixed = BRep_Builder(), set(), []
    for index in payload["targets"]["face"]:
        face = TopoDS.Face_s(fmap.FindKey(index))
        for edge in seams_of(face):
            dev = seam_deviation(face, edge)
            if dev is None or dev <= SEAM_DEV_MM:
                continue
            found = _iso_through_vertices(face, edge)
            if found is None:
                continue                        # a seam whose pcurve is not an iso: not this op's fault
            iso, s0, s1, shift = found
            reverse = s0 > s1
            # the seam's two pcurves (one per orientation) moved onto the iso through its vertices: same pairing,
            # same direction, so the face's wire is unchanged
            along_u = abs(BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0).Value(
                BRep_Tool.Range_s(edge, face)[0]).X() - BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0).Value(
                BRep_Tool.Range_s(edge, face)[1]).X()) <= 1e-9
            vec = gp_Vec2d(shift, 0.0) if along_u else gp_Vec2d(0.0, shift)
            fwd = BRep_Tool.CurveOnSurface_s(TopoDS.Edge_s(edge.Oriented(TopAbs_FORWARD)), face, 0.0, 0.0)
            rev = BRep_Tool.CurveOnSurface_s(TopoDS.Edge_s(edge.Oriented(TopAbs_REVERSED)), face, 0.0, 0.0)
            a, b = BRep_Tool.Range_s(edge, face)
            curve = Geom_TrimmedCurve(iso.Reversed() if reverse else iso,
                                      *((iso.ReversedParameter(s0), iso.ReversedParameter(s1)) if reverse
                                        else (s0, s1)))
            builder.UpdateEdge(edge, curve, TopLoc_Location(), 1e-7)
            builder.UpdateEdge(edge, fwd.Translated(vec), rev.Translated(vec), face, 1e-7)
            builder.Range(edge, a, b, False)
            builder.SameParameter(edge, False)
            ShapeFix_Edge().FixSameParameter(edge, 1e-7)
            after = seam_deviation(face, edge)
            j = emap.FindIndex(edge)
            fixed.append({"edge": j, "face": index, "dev_before_mm": dev, "dev_after_mm": after})
            force.add(("edge", j))
    if not fixed:
        raise KernelDeclined("no_change", f"no seam off its surface on face(s) {payload['targets']['face']}")
    params = {"faces": list(payload["targets"]["face"]), "seams": fixed}
    return _finish(payload, "rebuild_seam", params, old, base, base, _history(), force, start)


def _history() -> Any:
    from OCP.BRepTools import BRepTools_History

    return BRepTools_History()


# ---------------------------------------------------------------- parent half (no OCCT)

class RebuildBoundaryEdge:
    name = "rebuild_boundary_edge"
    #: aimed only by a v5 class menu (the detector's faces); not offered to v4's search_ai
    v5_class_only = True
    params_schema: dict = {"type": "object", "properties": {}, "additionalProperties": False}

    def applies_to(self, site: Site) -> bool:
        return any(e.startswith("F") for e in site.entities)

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, "v5_rebuild_boundary_edge", {"targets": _split(ws, targets, "face")}, timeout_s)


class RebuildSeam:
    name = "rebuild_seam"
    v5_class_only = True
    params_schema: dict = {"type": "object", "properties": {}, "additionalProperties": False}

    def applies_to(self, site: Site) -> bool:
        return any(e.startswith("F") for e in site.entities)

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        return _run(pool, ws, "v5_rebuild_seam", {"targets": _split(ws, targets, "face")}, timeout_s)


OPS: tuple[Any, ...] = (RebuildBoundaryEdge(), RebuildSeam())
