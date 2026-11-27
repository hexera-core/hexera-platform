# Responsibility: CRS v5 step 9's rule repair for a SURFACE CROSSING: imprint the intersection curve of crossing faces
#                 on both of them, so the meshers see one shared curve instead of two triangulations passing through
#                 each other.
# Owns: `ImprintCrossing` (op `imprint_crossing`) and its kernel function `op_imprint_crossing`.
# Boundaries: kernel side runs in a worker (OCP imported lazily); the guard and the history record are the split
#             family's (`split._finish`: solid count kept, a valid part stays valid). WHICH faces cross is measured by
#             v5's diagnosis (`repair_v5.crossing`); this op re-finds the partners of the faces it is aimed at itself,
#             so it also works from a mesher's one named surface.
"""Imprint a crossing: BRepAlgoAPI_Section of each target face with every face it really crosses, then
BRepAlgoAPI_Splitter of the whole part by those section edges. Both faces of a crossing pair are split along the SAME
edge, so the two discretisations share it. Exact: no surface moves, the deviation is zero.

A partner is a face whose box meets the target's, that shares no edge with it (neighbours meet along their common
edge, which is not a crossing), and whose section with the target has an edge running through the INTERIOR of at
least one of the two faces (a section along both boundaries imprints nothing)."""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
from meshpipeline.cad.repair_v4.ops import check_call, register, run_kernel_op
from meshpipeline.cad.repair_v4.ops.split import _finish
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

_FACE_ID = {"type": "string", "x-entity": "face"}
#: a section edge shorter than this share of the part's diagonal is a touch, not a crossing
MIN_SECTION_REL = 1e-6


def _interior(edge: Any, faces: list[Any], tol: float) -> bool:
    """The edge's midpoint lies strictly inside at least one of `faces` (not on its boundary)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN
    from OCP.TopoDS import TopoDS

    curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    mid = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()))
    for face in faces:
        f = TopoDS.Face_s(face)
        proj = GeomAPI_ProjectPointOnSurf(mid, BRep_Tool.Surface_s(f))
        if proj.NbPoints() < 1:
            continue
        u, v = proj.LowerDistanceParameters()
        if BRepClass_FaceClassifier(f, gp_Pnt2d(u, v), tol).State() == TopAbs_IN:
            return True
    return False


def _length(edge: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    return float(props.Mass())


def crossing_edges(shape: Any, targets: list[int], *, max_partners: int, diag: float,
                   only: int | None = None) -> tuple[list[Any], list[dict]]:
    """(the section edges through the interior of a target face and a partner, one row per pair crossed:
    {a, b, edges, length_mm}) for the target face indices of `shape`; `only` restricts the partners to one face."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel
    from meshpipeline.cad.repair_v5.diagnose_kernel import _shares_edge

    faces = ws_kernel._maps(shape)["face"]
    tol = max(1e-7, 1e-7 * diag)
    boxes: dict[int, Any] = {}

    def box(i: int) -> Any:
        if i not in boxes:
            b = Bnd_Box()
            BRepBndLib.Add_s(faces.FindKey(i), b, False)
            b.Enlarge(tol)
            boxes[i] = b
        return boxes[i]

    edges: list[Any] = []
    rows: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for a in targets:
        if not 1 <= a <= faces.Extent():
            continue
        fa = faces.FindKey(a)
        found = 0
        for b in ([only] if only is not None else range(1, faces.Extent() + 1)):
            key = (min(a, b), max(a, b))
            if b == a or key in seen or box(a).IsOut(box(b)):
                continue
            fb = faces.FindKey(b)
            if _shares_edge(fa, fb):
                continue
            section = BRepAlgoAPI_Section(fa, fb)
            section.Build()
            if not section.IsDone():
                continue
            found_edges = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(section.Shape(), TopAbs_EDGE, found_edges)
            mine = [found_edges.FindKey(i) for i in range(1, found_edges.Extent() + 1)]
            mine = [e for e in mine if _length(e) > MIN_SECTION_REL * diag and _interior(e, [fa, fb], tol)]
            if not mine:
                continue
            seen.add(key)
            edges.extend(mine)
            rows.append({"a": a, "b": b, "edges": len(mine), "length_mm": round(sum(_length(e) for e in mine), 6)})
            found += 1
            if found >= max_partners:
                break
    return edges, rows


@register_kernel_fn("op_imprint_crossing")
def _imprint_crossing(payload: dict) -> dict:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.TopTools import TopTools_ListOfShape

    from meshpipeline.cad.repair_v4 import ws_kernel

    start = time.perf_counter()
    old = ws_kernel.read_brep(payload["brep_path"])
    diag = ws_kernel.part_diagonal(payload, old)
    edges, rows = crossing_edges(old, [int(i) for i in payload["faces"]], max_partners=int(payload["max_partners"]),
                                 diag=diag)
    if not edges:
        return {"declined": "no_change: no face crosses the target faces through their interior"}
    tools = TopTools_ListOfShape()
    for e in edges:
        tools.Append(e)
    arguments = TopTools_ListOfShape()
    arguments.Append(old)
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.SetRunParallel(False)   # identical runs must give identical shapes
    try:
        splitter.Build()
    except Exception as exc:  # noqa: BLE001 - a kernel refusal is a decline
        return {"declined": f"split_failed: the splitter raised {type(exc).__name__}"}
    if not splitter.IsDone():
        return {"declined": "split_failed: the splitter did not complete"}
    params = {"max_partners": int(payload["max_partners"]),
              "measured": {"pairs": rows, "section_length_mm": round(sum(r["length_mm"] for r in rows), 6)}}
    return _finish(old, splitter.Shape(), splitter.History(), payload, op="imprint_crossing", params=params,
                   start=start)


@register
class ImprintCrossing:
    """Imprint the intersection curve of crossing faces on both faces (v5 step 9, surface_crossing's rule seed).
    Targets: the crossing faces (1+); the op finds each one's crossing partners itself, at most `max_partners`."""

    name = "imprint_crossing"
    params_schema = {
        "type": "object",
        "properties": {"max_partners": {"type": "integer", "minimum": 1, "maximum": 16}},
        "required": ["max_partners"],
        "additionalProperties": False,
        "x-targets": {"kind": "face", "min": 1, "max": 32},
    }

    def applies_to(self, site: Site) -> bool:
        return False      # never proposed by v4's sweeps: v5's surface_crossing menu aims it

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        check_call(self.name, targets, params, ws)
        return run_kernel_op(pool, ws, kernel_fn="op_imprint_crossing", op=self.name,
                             payload={"faces": [ws.indices[t] for t in targets],
                                      "max_partners": int(params["max_partners"])},
                             timeout_s=timeout_s)
